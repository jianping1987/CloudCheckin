# 樱花 SakuraFrp 自动签到（原计划部署、只跑樱花）

按上游 [timerring/CloudCheckin](https://github.com/timerring/CloudCheckin) 的原架构部署，
但只保留樱花一个平台：CircleCI 跑签到脚本，Cloudflare Worker 每天定时触发。

## 架构

```
Cloudflare Worker（每天 00:00 UTC 定时，见 wrangler.toml）
  → POST CircleCI custom webhook
    → CircleCI 跑 .circleci/config.yml 里的 sakurafrp job
      → python -m sakurafrp.sakurafrp 登录樱花、过 GeeTest 验证码、签到领流量
```

## 需要准备的东西（共 7 个必填 + 2 个可选）

先在下面 3 个地方注册 / 拿到：

### 1. CircleCI（https://circleci.com/，免费版够用）
- 用 GitHub 账号登录，在 CircleCI 里添加你的 Fork 仓库为 Project，
  **必须用 GitHub App 方式接入**（custom webhook 只支持 GitHub App 项目，不支持旧的 OAuth 方式）。
- Personal API Token：https://app.circleci.com/settings/user/tokens 申请一个
  → GitHub Secret `CIRCLECI_TOKEN`
- Organization ID：CircleCI 左侧 Organization Settings 里复制
  → GitHub Secret `CIRCLECI_ORG_ID`

### 2. CircleCI custom webhook（最容易错的一步）
在 CircleCI 网页里：选你的组织 → Projects → 找到 CloudCheckin 项目 →
Project Settings → Project Setup → 如果没有 pipeline 点 Add Pipeline →
在 pipeline 框底部点 **Custom webhook +**：
- 名字随便填，例如 `sakura-daily`
- Event source 随便填，例如 `cloudflare-worker`
- Branch 填 `main`
- 点 Save，会生成一个 **Webhook URL** 和一个 **secret**（secret 只显示一次，马上复制）

`CIRCLECI_WEBHOOK_URL` 的填法是两者拼起来：

```
<Webhook URL>?secret=<secret>
```

例如 `https://internal.circleci.com/private/soc/e/xxxx-xxxx?secret=yyyy`
→ GitHub Secret `CIRCLECI_WEBHOOK_URL`

### 3. Cloudflare（https://dash.cloudflare.com/）
- Profile → API Tokens → Create Token → 用 `Edit Cloudflare Workers` 模板
  → GitHub Secret `CLOUDFLARE_API_TOKEN`

### 4. 樱花 + 智谱（签到本身要用的）
| Secret 名 | 内容 |
|---|---|
| `SAKURAFRP_USERNAME` | 樱花账号用户名（https://www.natfrp.com） |
| `SAKURAFRP_PASSWORD` | 樱花账号密码 |
| `SAKURAFRP_VISION_APIKEY` | 识图模型 API Key：默认智谱 GLM-4V-Flash 免费版，在 https://open.bigmodel.cn/ 注册领 key（模型 `glm-4v-flash`，用于识别九宫格验证码，国内直连，不用翻墙） |
| `TELEGRAM_TOKEN`（可选） | Telegram 机器人 token |
| `TELEGRAM_CHAT_ID`（可选） | 你的 Telegram chat id |

也可以继续用旧名字 `SAKURAFRP_MIMO_APIKEY`：把它的值换成智谱 key 就行，不用改同步流程。想换别的识图接口（如 Gemini）可再设 `SAKURAFRP_VISION_BASE_URL` / `SAKURAFRP_VISION_MODEL`。

上面 9 个全部填到你的 Fork 仓库：Settings → Secrets and variables → Actions → New repository secret。
**账号密码和 key 只填在 Secrets 里，不要写进代码、不要提交到仓库。**

## 部署步骤（按顺序）

1. 把本目录所有文件推到你的 Fork 仓库，覆盖原文件（`.circleci/config.yml` 已精简为只跑 sakurafrp）。
2. 按上面填好 9 个 GitHub Secrets。
3. Actions → `Setup CircleCI Context and Secrets` → Run workflow。
   这一步会用 CircleCI CLI 在 CircleCI 里新建 `checkincontext`，并把樱花那几个 Secrets 同步过去。
   **以后每次改樱花账号密码或识图 key，都要先改 GitHub Secrets，再手动跑一次这个 workflow 同步，否则 CircleCI 用的还是旧的。**
4. Actions → `Deploy Cloudflare Worker` → Run workflow。
   这一步会把 `worker.js` 部署到 Cloudflare，并把 `CIRCLECI_WEBHOOK_URL` 写进 Worker 的 secret。
5. 验证：去 CircleCI 网页，手动触发一次 pipeline（或等 Worker 定时触发），
   看 sakurafrp job 的日志出现 `SakuraFrp check-in successful` 或 `Already signed in today`，
   再去樱花官网确认流量增加了。

## 定时时间

`wrangler.toml` 里现在是 `crons = ["0 0 * * *"]`，即每天 UTC 00:00，
**北京时间 08:00**（原仓库注释写的是 UTC 2:00 / 北京时间 10:00，那是注释写错了，
以 cron 表达式为准）。想改时间就改这个字段后重新跑第 4 步。

## 常见报错

| 报错 / 现象 | 原因 / 办法 |
|---|---|
| `Setup CircleCI` workflow 失败，提示 token / org id 无效 | `CIRCLECI_TOKEN` 或 `CIRCLECI_ORG_ID` 填错，重新复制 |
| Worker 部署成功但每天不触发 | `CIRCLECI_WEBHOOK_URL` 拼错（漏了 `?secret=`），或 webhook 是在 OAuth 项目上建的，custom webhook 只支持 GitHub App 项目 |
| CircleCI job 提示 `Set SAKURAFRP_USERNAME + SAKURAFRP_PASSWORD` | Secrets 没同步到 CircleCI：先确认 GitHub Secrets 填了，再重新跑 `Setup CircleCI Context and Secrets` |
| `Login captcha failed after 10 attempts` | 识图 key 无效 / 免费额度用完，检查 `SAKURAFRP_VISION_APIKEY`（智谱免费额度在 open.bigmodel.cn 控制台里看），同步后重跑 |
| `Login verification failed` | 樱花账号密码错，或登录接口变了 |
| 其他平台的 job 失败 | 本版已删掉其他平台，只剩 sakurafrp；如果还看到别的 job，说明 `.circleci/config.yml` 没覆盖成功 |
