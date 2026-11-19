"""
SakuraFRP auto check-in (pure HTTP protocol, no browser required).

Reverse-engineers GeeTest v3 protocol (AES-CBC + RSA encryption) for the
captcha handshake, then uses a multimodal AI to recognise the 9-grid
image captcha (Zhipu GLM-4V-Flash free tier by default).  Entire flow
completes in 2-3 seconds per attempt — fast enough for CircleCI.
"""

import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx
from dotenv import load_dotenv
from openai import OpenAI

from telegram.notify import send_source_notification

from .geetest_crack import GeeTestCrack

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

MOUSE_PATH_FILE = str(Path(__file__).resolve().parent / "mousepath.json")
MAX_CAPTCHA_RETRIES = 10

COMMON_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": '"Chromium";v="145", "Not:A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}

OPENID_AJAX_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "x-requested-with": "XMLHttpRequest",
}

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class NatfrpError(RuntimeError):
    """Base error for the SakuraFrp check-in flow."""


@dataclass(frozen=True)
class NatfrpConfig:
    username: str = ""
    password: str = ""
    vision_apikey: str = ""
    vision_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    vision_model: str = "glm-4v-flash"

    @classmethod
    def from_env(cls) -> "NatfrpConfig":
        username = os.environ.get("SAKURAFRP_USERNAME", "").strip()
        password = os.environ.get("SAKURAFRP_PASSWORD", "").strip()
        if not username or not password:
            raise NatfrpError("Set SAKURAFRP_USERNAME + SAKURAFRP_PASSWORD")
        # New name first; the old MiMo secret name still works, so the
        # existing CircleCI sync needs no workflow change — just replace
        # its value with the Zhipu key.
        vision_apikey = (
            os.environ.get("SAKURAFRP_VISION_APIKEY", "").strip()
            or os.environ.get("SAKURAFRP_MIMO_APIKEY", "").strip()
        )
        vision_base_url = (
            os.environ.get("SAKURAFRP_VISION_BASE_URL", "").strip()
            or "https://open.bigmodel.cn/api/paas/v4"
        )
        vision_model = (
            os.environ.get("SAKURAFRP_VISION_MODEL", "").strip()
            or "glm-4v-flash"
        )
        return cls(
            username=username,
            password=password,
            vision_apikey=vision_apikey,
            vision_base_url=vision_base_url,
            vision_model=vision_model,
        )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _create_client(follow_redirects: bool = False) -> httpx.Client:
    return httpx.Client(
        follow_redirects=follow_redirects,
        timeout=30.0,
        headers=COMMON_HEADERS,
    )


def _prime_openid_session(client: httpx.Client) -> None:
    """Follow the full OAuth redirect chain: natfrp -> /oauth/authorize -> OpenID login."""
    resp = client.get("https://www.natfrp.com/cgi/user/login")
    step = 0
    while resp.is_redirect and step < 10:
        location = resp.headers.get("location")
        if not location:
            break
        next_url = urljoin(str(resp.url), location)
        resp = client.get(next_url, headers={"referer": str(resp.url)})
        step += 1


def _follow_openid_redirect(client: httpx.Client) -> None:
    """Bridge the OpenID -> natfrp cross-domain login redirect chain."""
    resp = client.get(
        "https://openid.13a.com/redirect",
        headers={"referer": "https://openid.13a.com/login"},
    )
    step = 0
    while resp.is_redirect and step < 10:
        location = resp.headers.get("location")
        if not location:
            break
        next_url = urljoin(str(resp.url), location)
        resp = client.get(next_url, headers={"referer": str(resp.url)})
        step += 1


# ---------------------------------------------------------------------------
# AI captcha recognition (vision model)
# ---------------------------------------------------------------------------


def _download_image(url: str) -> bytes:
    """Download a captcha image from a GeeTest CDN URL."""
    resp = httpx.get(url, timeout=15.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _call_vision(image_url: str, prompt: str, config: NatfrpConfig) -> str:
    """Download a captcha image and send it to the vision model for recognition."""
    img_bytes = _download_image(image_url)
    b64 = base64.b64encode(img_bytes).decode("utf-8")

    client = OpenAI(
        api_key=config.vision_apikey,
        base_url=config.vision_base_url,
    )
    kwargs: dict = {
        "model": config.vision_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "max_completion_tokens": 1024,
    }
    # MiMo-only option: disable its built-in thinking.
    if "xiaomimimo" in config.vision_base_url:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content or ""
    return content.strip()


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        return text[7:-3].strip()
    if text.startswith("```") and text.endswith("```"):
        return text[3:-3].strip()
    return text


def _recognize_nine_grid(image_url: str, config: NatfrpConfig) -> list:
    """
    Recognise a 9-grid captcha via the vision model.
    Returns a list of col_row strings, e.g. ["1_1", "3_2"].
    """
    if not config.vision_apikey:
        print("  [AI] no SAKURAFRP_VISION_APIKEY configured", flush=True)
        return []

    prompt = (
        "这是一个九宫格验证码。请按从左到右、从上到下的顺序识别每个格子里的物品名称，"
        "最后识别左下角的参考图。输出格式为JSON："
        '{"1":"名称","2":"名称",...,"10":"参考图名称"}。'
        "名称要简洁，参考图名称必须是九宫格里已有的名称。"
        "若有类似物品（如气球与热气球），请统一名称。"
        "只输出JSON，不要其他文字。"
    )
    result_text = _strip_json_fence(_call_vision(image_url, prompt, config))
    print(f"  [AI] raw: {result_text}", flush=True)

    try:
        recognition = json.loads(result_text)
    except json.JSONDecodeError:
        print("  [AI] JSON parse failed", flush=True)
        return []

    target_name = recognition.get("10", "").strip()
    if not target_name:
        print("  [AI] could not get reference image name", flush=True)
        return []
    print(f"  [AI] target = '{target_name}'", flush=True)

    points = []
    for i in range(9):
        position = str(i + 1)
        item_name = recognition.get(position, "").strip()
        if item_name == target_name:
            col, row = i % 3 + 1, i // 3 + 1
            points.append(f"{col}_{row}")
            print(f"  [AI] match! pos {position}: {item_name} -> {col}_{row}", flush=True)
    return points


def _recognize_icon(image_url: str, config: NatfrpConfig) -> list:
    """Recognise an icon/space captcha, returning click coordinate strings."""
    if not config.vision_apikey:
        print("  [AI] no SAKURAFRP_VISION_APIKEY configured", flush=True)
        return []

    prompt = (
        "这是一个点选验证码。请识别图中需要点击的目标文字或图标，"
        "并返回每个目标的大致坐标位置。输出格式为JSON列表："
        '[{"x":100,"y":200},...]。坐标原点在左上角。'
        "只输出JSON，不要其他文字。"
    )
    result_text = _strip_json_fence(_call_vision(image_url, prompt, config))
    print(f"  [AI] raw: {result_text}", flush=True)

    try:
        coords = json.loads(result_text)
        if isinstance(coords, dict):
            coords = coords.get("coordinates", coords.get("points", []))
        points = []
        for c in coords:
            if isinstance(c, dict):
                if "x" in c and "y" in c:
                    points.append(f"{c['x']}_{c['y']}")
                elif "point_2d" in c:
                    pt = c["point_2d"]
                    points.append(f"{pt[0]}_{pt[1]}")
        return points
    except (json.JSONDecodeError, KeyError, TypeError, IndexError):
        print("  [AI] coordinate parse failed", flush=True)
        return []


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def _login(config: NatfrpConfig) -> httpx.Client:
    """Pure HTTP login: OAuth -> GeeTest handshake -> POST credentials -> bridge."""
    print("[1/2] Logging in ...", flush=True)

    client = _create_client(follow_redirects=False)
    _prime_openid_session(client)

    # get GeeTest config from OpenID
    resp = client.get(
        "https://openid.13a.com/cgi/captcha?login",
        headers={"referer": "https://openid.13a.com/login", **OPENID_AJAX_HEADERS},
    )
    captcha_data = resp.json()
    if not captcha_data.get("success"):
        raise NatfrpError(f"Failed to get captcha config: {captcha_data}")

    gt = captcha_data["message"]["gt"]
    challenge = captcha_data["message"]["challenge"]
    print(f"  gt={gt[:16]}... challenge={challenge[:16]}...", flush=True)

    # GeeTest handshake
    crack = GeeTestCrack(gt, challenge, MOUSE_PATH_FILE,
                         referer="https://openid.13a.com/")
    crack.get_type()
    crack.get_c_s()
    ajax_result = crack.ajax()

    validate = ajax_result.get("validate")
    if ajax_result.get("result") == "click":
        validate = _solve_captcha_loop(crack, config,
                                       "Login captcha")
    elif not validate:
        crack.close()
        raise NatfrpError(
            f"Login captcha returned no validate: {ajax_result}"
        )

    # POST login
    login_data = {
        "username": config.username,
        "password": config.password,
        "geetest_id": challenge,
        "geetest_challenge": challenge,
        "geetest_validate": validate,
        "geetest_seccode": f"{validate}|jordan",
    }
    resp = client.post(
        "https://openid.13a.com/cgi/password/login",
        data=login_data,
        headers={
            "referer": "https://openid.13a.com/login",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            **OPENID_AJAX_HEADERS,
        },
    )
    crack.close()

    if resp.status_code not in (200, 302):
        raise NatfrpError(
            f"Login request failed: {resp.status_code} {resp.text[:200]}"
        )

    _follow_openid_redirect(client)

    # verify login
    resp = client.get("https://www.natfrp.com/cgi/v4/user/info")
    user_info = resp.json()
    if "name" not in user_info and "id" not in user_info:
        raise NatfrpError(f"Login verification failed: {user_info}")

    print(f"  Logged in as: {user_info.get('name', 'unknown')}", flush=True)
    return client


# ---------------------------------------------------------------------------
# captcha solve loop (shared by login + sign-in)
# ---------------------------------------------------------------------------


def _solve_captcha_loop(crack: GeeTestCrack, config: NatfrpConfig,
                        label: str) -> str:
    """Solve image captcha in a retry loop.

    Returns the validate string on success.
    Raises NatfrpError if all attempts are exhausted.
    """
    for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
        print(f"  {label} attempt {attempt}/{MAX_CAPTCHA_RETRIES}", flush=True)
        pic_type, pic_url = crack.get_pic(
            0 if attempt == 1 else attempt
        )
        print(f"  pic_type: {pic_type}", flush=True)

        if pic_type == "nine":
            points = _recognize_nine_grid(pic_url, config)
        else:
            points = _recognize_icon(pic_url, config)

        if not points:
            time.sleep(2)
            continue
        print(f"  points: {points}", flush=True)

        result = crack.verify(points)
        if (result.get("status") == "success"
                and result.get("data", {}).get("result") == "success"):
            return result["data"]["validate"]
        time.sleep(2)

    raise NatfrpError(
        f"{label} failed after {MAX_CAPTCHA_RETRIES} attempts"
    )


# ---------------------------------------------------------------------------
# check-in
# ---------------------------------------------------------------------------


def _traffic_remaining_gib(user_info: dict) -> str | None:
    """Remaining traffic in GiB, from user info's traffic [used, total] bytes.

    Returns e.g. "15.21", or None when the fields are missing/unexpected.
    """
    traffic = user_info.get("traffic")
    if not isinstance(traffic, (list, tuple)) or len(traffic) < 2:
        return None
    try:
        used, total = float(traffic[0]), float(traffic[1])
    except (TypeError, ValueError):
        return None
    remaining = max(total - used, 0.0)
    return f"{remaining / (1024 ** 3):.2f}"


def _do_sign_in(client: httpx.Client, config: NatfrpConfig) -> tuple[bool, str]:
    """Sign in via HTTP: check status -> GeeTest solve -> POST sign.

    Returns (success, detail): detail is a human-readable summary for the
    notification, e.g. the streak and the traffic reward.
    """
    print("[2/2] Signing in ...", flush=True)

    # already signed?
    resp = client.get("https://www.natfrp.com/cgi/v4/user/info")
    user_info = resp.json()
    sign_info = user_info.get("sign", {})
    if sign_info.get("signed"):
        days = sign_info.get("days", "?")
        print(f"  Already signed in today (streak: {days} days)", flush=True)
        detail = f"Already signed in today, streak: {days} days"
        remaining = _traffic_remaining_gib(user_info)
        if remaining is not None:
            detail = f"{detail}\nTraffic remaining: {remaining} GiB"
        return True, detail

    # get sign-in GeeTest config
    resp = client.get("https://www.natfrp.com/cgi/v4/user/sign?gt")
    try:
        geetest_data = resp.json()
    except Exception:
        match = re.search(r"\((.+)\)", resp.text, re.DOTALL)
        if match:
            geetest_data = json.loads(match.group(1))
        else:
            raise NatfrpError(
                f"Cannot parse sign geetest data: {resp.text[:200]}"
            )
    if "gt" not in geetest_data:
        raise NatfrpError(f"Sign geetest data malformed: {geetest_data}")

    gt = geetest_data["gt"]
    challenge = geetest_data["challenge"]
    print(f"  gt={gt[:16]}... challenge={challenge[:16]}...", flush=True)

    # GeeTest handshake + AI solve
    crack = GeeTestCrack(gt, challenge, MOUSE_PATH_FILE,
                         referer="https://www.natfrp.com/")
    crack.get_type()
    crack.get_c_s()
    crack.ajax()

    validate = _solve_captcha_loop(crack, config, "Sign captcha")
    crack.close()

    # POST sign-in
    sign_headers = dict(COMMON_HEADERS)
    sign_headers["content-type"] = "application/x-www-form-urlencoded"
    sign_headers["referer"] = "https://www.natfrp.com/user/"
    resp = client.post(
        "https://www.natfrp.com/cgi/v4/user/sign",
        data={
            "geetest_challenge": challenge,
            "geetest_validate": validate,
            "geetest_seccode": f"{validate}|jordan",
        },
        headers=sign_headers,
    )
    sign_result = resp.json()
    print(
        f"  sign response: {json.dumps(sign_result, ensure_ascii=False)}",
        flush=True,
    )

    success = isinstance(sign_result, str) or (
        isinstance(sign_result, dict) and sign_result.get("status") == "success"
    )
    if not success:
        return False, f"Sign-in failed: {sign_result}"

    # Reward text comes straight from the sign response when it is a string,
    # e.g. "运气不错，获得 2.5 GiB 流量。" — also fetch the updated streak
    # and the total remaining traffic.
    reward = sign_result if isinstance(sign_result, str) else ""
    streak = ""
    remaining = ""
    try:
        info = client.get("https://www.natfrp.com/cgi/v4/user/info").json()
        days = info.get("sign", {}).get("days")
        if days is not None:
            streak = f"Streak: {days} days"
        gib = _traffic_remaining_gib(info)
        if gib is not None:
            remaining = f"Traffic remaining: {gib} GiB"
    except Exception:
        pass
    detail = "\n".join(part for part in (reward, streak, remaining) if part)
    return True, detail or "Signed in successfully"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def check_in(config: NatfrpConfig) -> tuple[bool, str]:
    client = _login(config)
    try:
        return _do_sign_in(client, config)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _notify_if_configured(message: str) -> None:
    enabled = os.environ.get("SAKURAFRP_NOTIFY", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if (
        enabled
        and os.environ.get("TELEGRAM_TOKEN")
        and os.environ.get("TELEGRAM_CHAT_ID")
    ):
        send_source_notification("SAKURAFRP", message)


def main() -> int:
    load_dotenv()
    try:
        success, detail = check_in(NatfrpConfig.from_env())
        if success:
            message = "SakuraFrp check-in successful"
            if detail:
                message = f"{message}\n{detail}"
            print(message, flush=True)
            _notify_if_configured(message)
            return 0
        message = f"SakuraFrp check-in failed: {detail or 'unknown error'}"
        print(message, file=sys.stderr, flush=True)
        _notify_if_configured(message)
    except NatfrpError as exc:
        message = f"SakuraFrp check-in failed: {exc}"
        print(message, file=sys.stderr, flush=True)
        _notify_if_configured(message)
    except Exception as exc:
        message = (
            "SakuraFrp check-in failed unexpectedly "
            f"({type(exc).__name__})"
        )
        print(message, file=sys.stderr, flush=True)
        _notify_if_configured(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
