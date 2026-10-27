# SteadyApiPool

[English](README.md) | **简体中文**

一个独立的 Web 应用，为你的 Azure OpenAI / Foundry 模型部署提供一个**带故障转移的 AI 网关**，无需先学习网关
或 Foundry。它会找到你的模型部署，并把同一模型的部署组合成一个**后端池**。客户端在一个终结点上调用后端池；
网关把请求分配给各个成员（故障转移、轮询或加权），让持续失败的成员暂停一段时间，并在另一个成员上重试。

你在设置页上选择网关：

- **[Azure API 管理](https://learn.microsoft.com/azure/api-management/)（APIM）**，使用它原生的后端池、断路器和
  会话感知。应用使用你已有的 APIM 服务，或新建一个，并把每个后端池绑定到一个 API。
- **[LiteLLM](https://github.com/BerriAI/litellm) 网关**，一个开源的 AI 网关。应用把它部署到 Azure 容器应用或
  AKS 上，或连接你已在运行的网关，并为客户端提供 OpenAI 风格的模型名、别名和虚拟密钥。

![后端池的路由预览选项卡：客户端用一个模型名调用网关，后端池把请求分配到多个区域的 Foundry 部署，并带有冷却和重试](docs/images/pool-routing.png)

*客户端只用一个终结点，后面是多个模型部署：路由预览选项卡展示网关如何在后端池中分配流量；点击某个部署，就能
看到它故障或被限流时会发生什么。*

```
SteadyApiPool/
├── backend/    FastAPI 应用（pool_manager：共享代码、apim/ 和 litellm/）及测试；同时提供构建好的
│               前端
├── frontend/   React/Vite 界面（src/apim、src/litellm 和其余共享部分；构建到
│               backend/pool_manager/static）
├── infra/      Bicep：容器应用和 AKS 上的 LiteLLM 网关（infra/gateway），以及用 Easy Auth 和最小权限
│               角色把本应用托管在 App Service 上
├── deploy/     容器应用上的公开多租户应用（仅 APIM；独立的 azd 项目）
├── scripts/    本地启动脚本、doctor、演示服务器、性能测试、发布打包和网关模板构建
├── docs/       本 README 的截图
└── azure.yaml  azd 项目定义（App Service 托管）
```

第一次使用？[快速开始](#快速开始)需要十分钟到半小时（如果还没有网关，大部分时间是等待 Azure 创建网关），只需要
Python。

## 选择哪种网关？

| | Azure API 管理 | LiteLLM |
| --- | --- | --- |
| 何时选择 | 你的组织已经在运行 APIM，或想要一个带有策略、产品和门户的 Azure 托管服务 | 你想要一个带有模型名、别名和按密钥限制的 OpenAI 风格终结点，或一个可以在任何地方运行的开源网关 |
| 应用会设置什么 | 一个 APIM 服务（你已有的，或新建的基本 v2 服务），每个后端池一个 `Pool` 后端，以及一个绑定到它的 API | 容器应用上带 PostgreSQL 的 LiteLLM 代理（一步完成）或新的 AKS 群集，或连接你已有的网关；每个后端池一个模型组 |
| 客户端如何调用 | `https://<apim>.azure-api.net/<api-path>/chat/completions?api-version=…`，带上 `Ocp-Apim-Subscription-Key` | `https://<gateway>/v1/chat/completions`，使用虚拟密钥，并把后端池 ID（或别名）作为模型 |
| 失败处理 | 每个成员一个断路器，API 策略中的重试，可选的会话感知 | 每个成员的冷却，在另一个成员上重试，Responses API 的后续请求固定发往最初响应的部署 |
| 费用 | 服务所选的层（建议从基本 v2 开始；开发人员层没有 SLA），按小时计费 | 容器应用约每月 $46–115（开发）或 $171–865（生产）；新的 AKS 群集约每月 $190–1,200（标价） |
| 其他形式 | [公开应用](#公开部署到-azure-容器应用)：任何人都可以用自己的 Azure 帐户登录 | — |

两种网关以相同的标识规则管理同样的 Foundry 部署（网关用它的托管标识调用部署，不使用密钥），你也可以在设置页上
在两者之间切换。

## 快速开始

本指南在你自己的电脑上运行应用。你只需安装 Python，不需要 Azure CLI、Node.js、Docker、配置文件，也不需要
任何 APIM 或 LiteLLM 经验；应用会检查缺少什么，并和你一起补上。（如果想给团队一个共享的网址，请参阅
[部署到 Azure](#部署到-azure)。）

> [!NOTE]
> 截图为英文界面。浏览器语言为中文时界面显示中文，也可以用顶栏的语言按钮切换。下文使用中文界面上的按钮名称。

**你需要准备**

- **Python 3.11 或更高版本。** Windows 上运行 `winget install -e --id Python.Python.3.13`，
  macOS 上运行 `brew install python@3.13`，其他系统请参阅[各操作系统的前置条件](#各操作系统的前置条件)。
- **一个 Azure 帐户**：能管理一个已有的网关（APIM 服务或 LiteLLM 网关的容器应用），或能在某个订阅中创建网关
  （*参与者*），并能读取你的 Azure OpenAI / Foundry 资源（*读者*）。
- **一些 Azure OpenAI / Foundry 模型部署**，最好是同一个模型部署在多个区域。如果还没有，
  应用可以帮你创建（第 4 步）。

### 1. 下载并启动

从 [Releases](https://github.com/guming3d/SteadyApiPool/releases) 下载
`SteadyApiPool-<version>.zip`，解压后在解压出的文件夹中运行启动脚本：

```powershell
scripts\start-local.cmd        # Windows
```

```sh
scripts/start-local.sh         # macOS、Linux、WSL
```

首次运行会检查你的电脑，并把应用所需的依赖安装到本地的 `.venv` 中，大约需要一分钟。然后打开
**http://127.0.0.1:8000**。使用应用期间请保持窗口打开，按 Ctrl+C 停止。

### 2. 登录 Azure

点击**用浏览器登录**，用你的 Azure 帐户登录。在没有浏览器的机器上（SSH、WSL、服务器），点击
**改用验证码登录**。应用会把登录状态保存在操作系统的加密存储中，所以只需登录一次。

![设置页的登录、AI 网关、权限和网关 → Foundry 四步中的第 1 步：用浏览器登录，或改用验证码登录](docs/images/onboarding-1-sign-in.png)

### 3. 选择网关类型，然后选择网关

选择 **LiteLLM 网关**或 **Azure API 管理**（见[选择哪种网关？](#选择哪种网关)），之后也可以切换。应用随后会列出
你能访问的每个订阅中该类型的网关，并说明你是否有权管理它们。

![设置页第 2 步：选择 LiteLLM 网关或 Azure API 管理，以及订阅中的 LiteLLM 网关，每个都有“使用此网关”按钮，另有“部署网关”和“连接已有网关”](docs/images/onboarding-2-choose-gateway.png)

#### Azure API 管理

在你想要的 APIM 服务上点击**使用此服务**。

![设置页第 2 步，选择了 Azure API 管理：订阅中的 APIM 服务，每个都有“使用此服务”按钮，另有“新建 APIM 服务”](docs/images/onboarding-2-choose-apim.png)

还没有 APIM 服务？点击**新建 APIM 服务**。所有内容都已填好：唯一的名称、新的资源组、区域，以及推荐的
**基本 v2** 层。确认发布者电子邮件后点击**创建并使用**。应用会等到服务就绪，通常需要几分钟
（开发人员层需要 30–40 分钟）。服务从创建起就开始计费。

![新建 APIM 服务表单，已预填基本 v2 层](docs/images/onboarding-2b-create-apim.png)

#### LiteLLM 网关

在 Azure 容器应用上的某个 LiteLLM 网关上点击**使用此网关**。

还没有网关？点击**部署网关**。应用会先问**网关在哪里运行？**（见下文[容器应用还是 AKS？](#容器应用还是-aks)），
并给出推荐：

- **部署到容器应用**会打开一个所有内容都已填好的表单：唯一的名称、资源组、区域和规模（**开发 / 试用**或
  **生产**），并附有标价估算。点击**部署**。应用以你自己的 Azure 权限部署 LiteLLM、PostgreSQL、一个托管标识和
  Application Insights，并跟踪部署进度，通常需要 5–15 分钟（PostgreSQL 最慢）。期间可以关闭应用，设置会接着跟踪。
  资源从创建起就开始计费。
- **在 AKS 上部署**为**新的 AKS 群集**打开同样的表单。点击**部署**后，应用分三个阶段进行，并实时显示进度（通常共
  15–25 分钟）：
  1. **Azure 资源**：群集（Microsoft Entra ID 与 Azure RBAC、工作负载标识、托管 Gateway API）、PostgreSQL、网关
     的托管标识、Application Insights，以及生产配置的 Redis。你会获得该群集的 *Azure Kubernetes 服务 RBAC 群集管理员*
     角色，因此部署需要资源组上的所有者或用户访问管理员权限。
  2. **在群集中安装**：通过 AKS 运行命令（你的电脑上无需 kubectl 或 Helm）安装 cert-manager、OpenTelemetry
     Collector、LiteLLM 的 Helm 图表和 HTTPS 网关。
  3. **HTTPS 证书**：Let's Encrypt 为 `https://<名称>.<区域>.cloudapp.azure.com` 签发证书；LiteLLM 在该地址上
     应答后，应用就像连接任何网关一样连接它。

  运行期间请保持此页打开（进度会推动安装继续；关闭后回到 **AI 网关**这一步即可继续）。安装失败时会显示其输出，
  并提供**重新运行安装**。此后群集**由你维护**，见 [AKS 上的 LiteLLM 网关](#aks-上的-litellm-网关)。
- **使用已有群集（生成文件）**适用于你已有的群集：它会询问群集、其资源组和命名空间，然后生成 `values.yaml`、
  `deploy.sh` 和 `README.md`（Helm 图表、工作负载标识、PostgreSQL 灵活服务器）。群集需要启用 OIDC 颁发者和工作负载
  标识。运行该脚本（例如在 Azure Cloud Shell 中），然后点击**已部署，去连接**。脚本通过公共 IP 以纯 HTTP 公开
  LiteLLM，而**连接**只接受 HTTPS URL（纯 HTTP 仅限 localhost，主密钥不会以明文经过网络）。想在运行本应用的电脑上
  先试用，可保持 `kubectl -n <namespace> port-forward svc/litellm 4000:4000` 运行并连接 `http://localhost:4000`；
  正式使用时，请在它前面加上 HTTPS，并连接其 HTTPS URL。

**连接已有网关**适用于任何带数据库（`STORE_MODEL_IN_DB`）的 LiteLLM 代理：输入它的 HTTPS URL、主密钥，以及可选的
它调用 Foundry 时使用的托管标识对象 ID 和它的 Application Insights 资源 ID。应用会用网关本身验证 URL 和主密钥，
并把主密钥加密保存在本机，从不以明文写入文件。

![网关在哪里运行？几个问题，推荐 Azure 容器应用，以及 AKS 选项](docs/images/onboarding-2b-deploy-gateway.png)

接下来会打开一个两分钟的导览。可以跟着看，也可以跳过；顶栏的**导览**可以重新播放。

##### 容器应用还是 AKS？

问卷会询问网关的用途、峰值 TPM、每个请求的平均 token 数，以及三个是/否问题，然后推荐：

| | Azure 容器应用（默认） | Azure Kubernetes 服务（AKS） |
| --- | --- | --- |
| 何时选择 | 大多数情况：试用，以及每秒约 250 个请求以内的生产流量（生产配置 5 个副本的承载量） | 你已有 AKS 群集和运维它的团队；要在网关旁运行自托管（GPU）模型；或峰值超过每秒约 250 个请求（峰值 TPM ÷ 60 ÷ 每个请求的 token 数） |
| 如何部署 | 由本应用一步部署，无需运维群集 | 由本应用新建并配置好的群集（之后由你维护），或为已有群集生成的文件 |
| 规模 | **开发 / 试用**：1 个副本，无 Redis。**生产**：2–5 个副本按负载伸缩，通过 Redis 共享冷却状态 | **开发 / 试用**：1 个 Standard_D4ds_v5 节点，免费层，无 Redis。**生产**：3–6 个节点跨可用性区域，标准层（运行时间 SLA），2–6 个 LiteLLM 副本通过 Redis 共享冷却状态 |
| 标价 | 开发约**每月 $46–115**；生产约**每月 $171–865** | 新群集：开发约**每月 $190–230**；生产约**每月 $670–1,200**。已有群集：其费用，加上 PostgreSQL |

这些价格是**零售标价估算**（美元，美国东部 2，从空闲到繁忙），涵盖每种规模部署的内容：容器应用副本、PostgreSQL，
以及生产规模的 Redis。不包括免费额度、日志引入和折扣，当然也不包括模型用量，后者照常由 Foundry 计费。网关创建后，
它的面板会显示本月实际花费和所在区域的标价。对于生产规模，推荐还会说明峰值时需要规划多少个副本（大约每秒 50 个
请求一个副本）。

不能对互联网开放的网关在两者上都可以实现，但这里没有自动化专用网络：在容器应用上，之后把环境放进虚拟网络
（或修改模板）；在 AKS 上，使用内部负载均衡器或专用入口。

### 4. 选择要放进后端池的部署

打开**模型部署**选项卡。它按模型分组，列出网关所在订阅中的每个 Azure OpenAI / Foundry 部署。（其他订阅中的部署：
在 `backend/.env` 中设置 `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS`。）在你想要的模型上点击**用此模型创建后端池**。
如果只想放入某个模型的部分部署，勾选它们后选择**导入并创建后端池**。

![模型部署选项卡：gpt-5.6-luna 部署在三个区域，带有“用此模型创建后端池”按钮](docs/images/onboarding-3-deployments.png)

还没有任何部署？**部署模型**会在你的某个 Foundry 资源中创建部署，然后在这里列出。

### 5. 发布后端池

后端池打开时所有内容都已填好，默认值即可使用：

- **APIM：** ID、客户端将调用的 API 路径、成员和断路器。
- **LiteLLM：** ID（这里是 `pool-gpt-5-6-luna`，它也是客户端发送的模型名）、成员，以及冷却、重试和超时设置。

**故障转移**把所有流量发给主部署，在它失败或配额用尽时切换到备用部署；如果想把流量分摊到所有成员，请选择
**轮询**，或选择**加权**自行设置比例。

![添加后端池，已预填故障转移模式下的三个成员和冷却设置](docs/images/onboarding-4-create-pool.png)

点击**发布到 APIM**（或**发布到网关**）。应用会准确列出它将写入的内容。再次点击确认。更改会立即在网关上生效。

![确认发布：新的后端池及其三个成员](docs/images/onboarding-5-publish.png)

### 6. 允许网关调用你的模型，并获取密钥

后端池会打开到**调用测试**选项卡，它会带你完成剩下的步骤：

1. **全部授予**为网关的托管标识（APIM 的，或 LiteLLM 网关的）在每个 Foundry 资源上分配
   *Cognitive Services OpenAI User* 角色，这样网关无需 API 密钥即可调用你的模型。这需要一个能在这些资源上分配
   角色的角色（*所有者*、*用户访问管理员*或*基于角色的访问控制管理员*）。如果没有，顶栏的**权限检查**会给出一份
   可以直接发给 Azure 管理员的申请。在 APIM 上，如果服务的托管标识未启用，设置会先启用它。
2. **创建测试密钥**会获取一个只能调用此后端池的密钥：
   - **APIM：** 一个只能访问此后端池 API 的订阅密钥。
   - **LiteLLM：** 一个只能调用此后端池模型名的虚拟密钥。LiteLLM 只保存它的哈希，所以如果想保留，请现在复制：
     它只显示这一次。上方的**添加别名**可以让已经发送其他模型名（例如 `gpt-5.6-luna`）的客户端也调用到此后端池。

![调用测试选项卡：终结点与模型名、调用前检查，以及获取虚拟密钥和“创建测试密钥”](docs/images/onboarding-6-grant-and-key.png)

### 7. 发送测试请求

点击**发送请求**。请求会经过网关和后端池，你会得到模型的回答以及处理请求的成员（在 LiteLLM 上，还有它之前在
其他成员上尝试了几次）。如果刚刚授予访问权限，Azure 最多可能需要 10 分钟才能使角色生效；在此之前页面会解释错误
原因，请稍后重试。

![在这里测试：200，调用成功，由后端池的主成员 pool-aoai-westus3-gpt-5-6-luna-dz 响应](docs/images/onboarding-7-test-call.png)

你的应用像调用任何 OpenAI 终结点一样调用后端池。页面下方的**发送请求**部分提供 curl、Python、JavaScript 和
PowerShell 代码，并已填好你的 URL。通过 APIM 的 Python 示例：

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<your-apim>.azure-api.net/<api-path>",
    api_key="unused",  # SDK 需要一个值；APIM 会向 Azure OpenAI 发送它自己的令牌
    default_headers={"Ocp-Apim-Subscription-Key": "<your-subscription-key>"},
    default_query={"api-version": "2025-04-01-preview"},
)
response = client.chat.completions.create(
    model="gpt-5.6-luna", messages=[{"role": "user", "content": "Hello"}]
)
print(response.choices[0].message.content)
```

通过 LiteLLM（基础 URL 为 `<gateway>/v1`，虚拟密钥作为 API 密钥，后端池 ID 或别名作为模型名，不需要
`api-version`）：

```python
from openai import OpenAI

client = OpenAI(base_url="https://<your-gateway>/v1", api_key="<your-virtual-key>")

response = client.chat.completions.create(
    model="pool-gpt-5-6-luna",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

### 对后端池进行性能测试（可选，LiteLLM）

**在这里测试**下方的**性能测试**模拟多个用户同时调用后端池：每个用户发送请求、等待响应后再发下一个。可以选择
**快速检查**（2 个用户、30 秒）、**典型负载**（10 个用户、2 分钟）或**压力测试**（30 个用户、3 分钟），也可以在
**高级设置**中设置用户数、持续时间、逐步加压时间、速率上限、提示长度、`max_tokens` 和流式传输。页面会先估算请求数
和令牌数；每个请求都像真实流量一样由 Foundry 计费，也会占用部署的速率限制。运行期间可以看到吞吐量、成功率、
p50/p95/p99 延迟、首令牌时间、429 和故障转移、每秒图表以及响应的成员；结束后给出一句结论（例如所有成员同时返回
429，说明后端池需要更多容量），并可以**导出 JSON / CSV**，或复制在终端中运行同样测试的命令。每次测试最多 50 个
用户、10 分钟、5,000 个请求，同一时间只能运行一个，并且只有所有者可以启动。

在终端中运行（可用于此网关或任何兼容 OpenAI 的终结点；密钥放在环境变量中，不会留在命令历史里）：

```powershell
$env:LITELLM_KEY = "<your-virtual-key>"
backend\.venv\Scripts\python scripts\benchmark.py --url https://<your-gateway>/v1 --model pool-gpt-5-6-luna --preset typical
#   --users 20 --duration 120 --ramp-up 10 --rate 15 --prompt long --max-tokens 256 --no-stream
#   --json result.json --csv requests.csv        （macOS/Linux：backend/.venv/bin/python，export LITELLM_KEY=...）
```

它每秒打印一行状态，结束时打印报告；按 Ctrl+C 会停止测试（已发送的请求会完成），并仍然打印报告。`--i-know`
只在终端中解除上述限制。

### 8. 观察运行情况

**指标监控**选项卡显示请求数、可用性、返回给客户端的 429（在 LiteLLM 上，还有故障转移挽回了多少）和延迟，以及
每个成员承担的流量份额和令牌数。**路由预览**可以模拟成员故障，让你在真正发生之前看到故障转移的过程。

![指标监控选项卡：请求数、可用性、429、延迟和每个成员的流量份额](docs/images/onboarding-8-metrics.png)

就这些。其他功能（更多后端池、权重、会话感知或别名、绑定你自己的 API、自定义终结点、清理）都在同一个界面中；
[功能一览](#功能一览)列出了全部功能。

> [!CAUTION]
> 应用以**你**的身份操作 Azure，作用于你**真实**的网关（对于 LiteLLM，还会使用它的主密钥）。所有更改对所有客户端
> 立即生效。

**如果遇到问题**

- 顶栏的**权限检查**会重新运行设置检查。凡是你自己的 Azure 权限无法修复的问题，都会变成一份发给管理员的申请：
  角色、作用域、一条 `az` 命令和一个门户链接。
- `python scripts/doctor.py` 会说明你的电脑或网络有什么问题（Python 版本、代理、公司 TLS 证书、DNS），
  以及在你的操作系统上如何修复。
- 在代理后面或受限网络中？请参阅[网络代理与证书](#网络代理与证书)。

**不用 Azure 试用。** 在克隆的仓库中，`scripts/demo_server.py` 会让同一个界面连接一个内存中的 Azure、APIM 和
LiteLLM 网关，带有示例部署，上面的截图就是这样制作的。它不会访问 Azure。它需要后端的虚拟环境（启动脚本会创建）
和构建好的界面（Node.js 22+）：

```sh
cd frontend && npm ci && npm run build && cd ..
backend/.venv/Scripts/python scripts/demo_server.py    # macOS 和 Linux 上为 bin/python；http://127.0.0.1:8010
```

它像首次运行一样从未登录状态开始。`POST /demo/stage/{sign-in,ready,pool}?product=apim`（或 `litellm`）可以跳到
引导流程中的某个阶段（`pool` 是一个已在使用、带有一小时指标的后端池）；`--product` 和 `--stage` 决定它从哪里开始。

## 功能一览

| 功能 | 说明 |
| --- | --- |
| **设置向导** | 四个步骤：**登录**、网关（**AI 网关**或 **APIM 服务**）、**权限**和**网关 → Foundry**。它检查登录、网关（对于 LiteLLM：可访问、数据库已连接、主密钥有效）、网关的托管标识、你对网关的访问权限，以及网关对 Foundry 的访问权限。能用你的 Azure 权限修复的就直接修复，其余的生成一份可以直接发给管理员的申请。在本地还可以选择网关类型，并选择、新建、连接或删除网关。 |
| **网关选择** | APIM：已有的服务，或新建一个。LiteLLM：根据几个回答推荐容器应用或 AKS，可一步部署到容器应用，或新建 AKS 群集并在其上安装带 HTTPS 的 LiteLLM，或为已有群集生成部署文件，或连接任何 LiteLLM 代理。 |
| **导览** | 面向 AI 网关新手的两分钟页面导览。设置完成后自动运行一次；顶栏的**导览**可以重新播放。 |
| **部署发现** | 按模型分组，列出你的订阅中的 Azure OpenAI / Foundry 部署，并把选中的部署导入到新的或已有的后端池。 |
| **部署模型** | 用你自己的 Azure 权限在 Foundry 帐户中创建按令牌计费的 OpenAI 模型部署，无需打开 Foundry 门户。 |
| **后端池** | 创建和编辑包含 2–30 个成员的后端池，支持**故障转移**、**轮询**或**加权**模式。在 APIM 上，后端池是一个 `Pool` 后端，可选会话感知；在 LiteLLM 上，它是一个模型组，ID 就是客户端发送的模型名。 |
| **成员** | 在所属后端池中编辑。除了 Foundry 部署，还可以添加任何兼容 OpenAI 的终结点（在 LiteLLM 上，身份验证可以是无、由 LiteLLM 作为加密凭据保存的 API 密钥，或网关的托管标识）。不再被任何后端池使用时，应用创建的成员会被删除。 |
| **失败处理** | 后端池设置，写入每个成员。APIM：一条断路器规则。LiteLLM：冷却（一分钟内失败多少次会让成员暂停、暂停多久）、在另一个成员上重试，以及单次尝试的超时。 |
| **后端池 API（APIM）** | 创建后端池时会同时为它创建并绑定一个 HTTP API，客户端马上就有可调用的终结点。**API 绑定**通过策略把已有 API 指向后端池，写入前先预览差异；解除绑定会逐字节恢复原来的策略。 |
| **模型名和别名（LiteLLM）** | 后端池 ID 始终可用；别名把其他模型名指向此后端池，已有的客户端无需修改。**查看 LiteLLM 配置**以 LiteLLM `config.yaml` 的写法显示后端池（不含机密），发布前可查看差异，也可以**导出 YAML**。 |
| **路由预览** | 显示网关如何通过后端池路由流量，并模拟故障、断路器跳变或冷却，以及恢复。 |
| **调用测试** | 获取测试密钥（APIM 订阅密钥或 LiteLLM 虚拟密钥），在页面上发送测试请求并显示由哪个成员响应，还提供可直接复制的客户端代码。 |
| **性能测试（LiteLLM）** | 在调用测试选项卡或用 `scripts/benchmark.py` 模拟多个用户调用后端池：吞吐量、延迟和首令牌时间的百分位数、429、故障转移以及响应的成员，并给出结论，可以导出结果。 |
| **指标监控** | 后端池的请求数、可用性、429 和延迟（来自 APIM 的 Azure Monitor 指标，或 LiteLLM 网关在 Application Insights 中的 OpenTelemetry 跟踪），以及来自 Azure Monitor 的每个成员的流量份额、令牌数和延迟。 |
| **费用** | 网关资源本月的花费和标价。点击顶栏中的网关名称打开；在 LiteLLM 上，那里还有 LiteLLM 自带管理界面的链接。 |
| **同一模型** | 如果后端池中的 Foundry 成员提供不同的模型，则拒绝保存。模型从 Azure 读取，而不是根据部署名称猜测。 |
| **删除** | 删除一个后端池，或此应用在网关上创建的全部内容，删除前会准确列出将被删除的内容。在本地还可以删除此应用创建的网关（APIM 服务会被软删除，48 小时内可恢复；LiteLLM 网关会被永久删除）。 |
| **公开应用（APIM）** | 部署在容器应用上的网站，任何人都可以用自己的 Microsoft 帐户登录，并以自己的 Azure 权限工作；服务器不保存任何内容。 |
| **安全性** | 应用只更改和删除它自己创建的内容，并且只在你看过之后才这样做。在 APIM 上，每次更新都基于 ETag 条件请求，从不改写不是它写入的策略；在 LiteLLM 上，在其他地方添加的后端池以只读方式显示。带有受保护 ID 前缀（`POOL_MANAGER_PROTECTED_PREFIXES`）的资源在两种网关上都是只读的。 |

### 后端池在 APIM 中的对应关系

| 在应用中 | 在 APIM 中 |
| --- | --- |
| 后端池 | 一个类型为 `Pool` 的后端，其 `pool.services` 是各成员，带有 `priority` 和 `weight`，以及可选的 `sessionAffinity` |
| 成员 | 一个单独的后端，使用部署的 URL 和托管标识身份验证（APIM 自身的标识，资源为 `https://cognitiveservices.azure.com`） |
| 断路器 | 一条规则，写入此应用创建的每个成员后端 |
| 后端池 API | 一个 HTTP API，其策略把后端池设为后端、发送托管标识令牌并重试 |
| 测试密钥 | 一个限定为后端池 API 的订阅 |

新后端池的默认值：
- **断路器：** 60 秒内出现 1 次 429 / 408 / 5xx / 连接失败 / 超时即跳变，跳变持续 60 秒，并遵循 `Retry-After`。
- **重试：** 2 次，间隔 1 秒，第一次重试立即进行。
- **后端超时：** 120 秒。

`POOL_MANAGER_POOL_API_VERSION` 默认为 `2025-09-01-preview`：GA 版本 `2024-05-01` 无法携带 `sessionAffinity`，
因此使用它时会话感知处于关闭状态。

### 后端池在 LiteLLM 中的对应关系

| 在应用中 | 在 LiteLLM 中 |
| --- | --- |
| 后端池 | 一个模型组：每个成员一行模型，`model_name` 都是后端池 ID |
| 成员 | 一行 `litellm_params.model = azure/<deployment>`、`api_base` 为帐户终结点且不带密钥的模型：网关用它的托管标识登录（`enable_azure_ad_token_refresh`） |
| 优先级 / 权重 | `litellm_params.order`（优先级 + 1）和 `litellm_params.weight`。LiteLLM 使用成员未在冷却中的最小 order，并在其中按权重分配 |
| 冷却 | 后端池每一行上的 `model_info.allowed_fails` 和 `model_info.cooldown_time` |
| 重试 / 超时 | 每一行上的 `litellm_params.num_retries` 和 `timeout` |
| 别名 | `router_settings.model_group_alias` |
| 测试密钥 | 别名为 `<prefix>test-<pool>`、限定为后端池模型名的虚拟密钥 |

新后端池的默认值：
- **冷却：** 成员在一分钟内失败一次（429 也算）就暂停 60 秒；它的流量转给其他成员，故障转移模式下转给下一个优先级。
- **重试：** 在另一个成员上再尝试 1 次。
- **单次尝试超时：** 120 秒。

**关于 LiteLLM 需要知道的：**
- **没有 ETag。** LiteLLM 不支持条件更新，所以保存后端池是尽力而为，不是原子操作。应用会重新读取后端池的各行并
  与你加载时的内容比较（如果有人改过就拒绝），然后先写入新行，再更新，最后删除，所以后端池不会变空。各行的 ID
  是确定的，所以失败后再次发布就能完成保存。
- **密钥只显示一次。** LiteLLM 只保存每个虚拟密钥的哈希。就地重新生成密钥（`/key/regenerate`）是 LiteLLM
  企业版功能，所以**重新生成测试密钥**会删除旧的测试密钥并创建一个新的。
- **会话保持是网关级设置。** Responses API 的后续请求（`previous_response_id`、加密推理内容）会发回最初响应的
  部署。它在网关配置中对所有后端池生效，不能按后端池设置。
- **没有运行状况检查。** 只有真实的失败请求才会让成员进入冷却。所有成员都在冷却时，客户端会收到最后一个上游错误
  （429 仍是 429），所以客户端应退避后重试。
- 有多个副本时，冷却状态只能通过 Redis 共享；容器应用上的生产规模包含 Redis。

### 发现的部署

- **列出哪些部署：** 只列出 `OpenAI` 或 `AIServices` 帐户上状态为 `Succeeded` 的 **OpenAI 格式**部署。合作伙伴模型（Anthropic、DeepSeek、xAI 等）使用其他推理路径，会被跳过。
- **成员：** 每个导入的成员指向 `https://<account>.openai.azure.com/openai/deployments/<deployment>`（在 LiteLLM 上是一行 `azure/<deployment>`）。因此同一模型名称不同的部署，例如 `gpt-5.6-luna` 和 DataZone 的 `gpt-5.6-luna-dz`，可以放进同一个后端池。
- **客户端如何调用：**
  - APIM 会追加操作路径，所以客户端调用 `https://<apim>/<api-path>/chat/completions?api-version=…`，并带上请求头 `Ocp-Apim-Subscription-Key`。应用为后端池创建的 API 已经是这种形式。如果改为绑定你自己的 API，它必须公开 `/chat/completions`（或通配的 `/*`），而不是 `/deployments/{id}/…`。
  - LiteLLM：`https://<gateway>/v1/chat/completions`（或 `/v1/responses`），带上 `Authorization: Bearer <虚拟密钥>`，`model` 为后端池 ID 或别名。
- **身份验证：** 成员使用**网关的托管标识**进行身份验证，因为大多数帐户禁用了密钥身份验证。它需要每个帐户上的 **Cognitive Services OpenAI User** 角色；设置向导、模型部署选择栏和调用测试选项卡会检查这一点，并在你有权限时授予（**授予访问权限**），否则给出一份带有完整 `az role assignment create` 命令的管理员申请。在 APIM 上，还必须启用服务的系统分配托管标识（**启用托管标识**），并且你自己绑定的 API 必须发送 `https://cognitiveservices.azure.com` 的托管标识令牌。
- **缓存：** 结果缓存 5 分钟。**刷新**会重新扫描，25 个帐户大约需要 15 秒。

### 容器应用上的 LiteLLM 网关

**部署网关**会在你选择的资源组中创建以下资源（`infra/gateway/litellm-aca.bicep`，编译为
`backend/pool_manager/templates/litellm-aca.json`）。所有资源都标记 `managed-by: ai-gateway-pool` 和
`ai-gateway: <name>`。

| 资源 | 说明 |
| --- | --- |
| 容器应用 `<name>` | LiteLLM `v1.106.4`（固定映像 `ghcr.io/berriai/litellm:v1.106.4`），端口 4000，通过外部 HTTPS 入口公开，另有一个 OpenTelemetry Collector 边车容器把模型调用的跟踪发送到 Application Insights。开发规模 1 个副本；生产规模 2–5 个副本。主密钥、盐密钥、数据库 URL 和 `config.yaml` 都是容器应用机密 |
| 容器应用环境 `cae-<name>` | 消耗计划，日志发送到 Log Analytics |
| 用户分配的托管标识 `id-<name>` | LiteLLM 调用 Foundry 时使用的身份（`AZURE_CLIENT_ID`）；设置向导会在成员帐户上为它分配 *Cognitive Services OpenAI User*。它没有其他角色 |
| Azure Database for PostgreSQL 灵活服务器 | LiteLLM 的模型表、虚拟密钥和花费记录。可突增 B1ms（开发）或 B2s（生产），32 GB。公共网络访问，仅允许 Azure 服务 |
| Azure 托管 Redis（仅生产） | Balanced B0，让各副本共享冷却状态 |
| Log Analytics 工作区 `log-<name>`、Application Insights `appi-<name>` | 容器日志，以及指标监控选项卡所用的跟踪 |

网关的机密由应用在部署时生成一次，之后应用从不轮换：盐密钥用于加密 LiteLLM 保存的成员密钥，因此不能更改。
这也是为什么已存在的网关绝不会被重新部署覆盖。

**删除网关**（网关面板中的**删除网关**，或网关列表中的**删除**）仅限此应用部署的网关，并需要再次输入名称。它会
删除标记为该网关的资源（先删除容器应用），从不删除资源组。**没有软删除**：Azure 不保留副本，数据库连同其中的
后端池、密钥和花费记录都会丢失。连接的网关绝不会被删除；**断开连接**只会让应用忘记它和它的密钥。

### AKS 上的 LiteLLM 网关

**在 AKS 上部署**会创建以下内容（`infra/gateway/litellm-aks.bicep`，编译为
`backend/pool_manager/templates/litellm-aks.json`，随后是 `litellm/gateway_template.py` 中 `aks_bootstrap` 的
安装）。Azure 资源的标记与容器应用网关相同。

| 资源 | 说明 |
| --- | --- |
| AKS 群集 `<名称>` | Microsoft Entra ID 与 Azure RBAC（无本地帐户），OIDC 颁发者和工作负载标识，Azure CNI 覆盖网络，Azure Linux 节点（Standard_D4ds_v5）。入口使用托管 Gateway API 和应用程序路由的 Istio 实现（托管 NGINX 入口已停用）。补丁版本和节点映像自动升级 |
| 群集中的 `litellm` 命名空间 | LiteLLM 的 Helm 图表（固定为 `1.106.4`，使用工作负载标识）、把模型调用跟踪发送到 Application Insights 的 OpenTelemetry Collector，以及位于公共 IP 上、DNS 名称为 `<名称>.<区域>.cloudapp.azure.com` 的网关。cert-manager（位于 `cert-manager`）获取并续订 Let's Encrypt 证书。纯 HTTP 只会重定向到 HTTPS |
| 用户分配的托管标识 `id-<名称>` | 与 LiteLLM 的服务帐户联合；LiteLLM 以它的身份调用 Foundry。设置向导会在成员帐户上为它授予 *Cognitive Services OpenAI User* |
| PostgreSQL、Redis（生产）、Log Analytics、Application Insights | 与容器应用相同 |
| 角色分配 | 你在该群集上的 *Azure Kubernetes 服务 RBAC 群集管理员* |

机密（主密钥、盐密钥、Postgres 密码）由应用生成，安装完成前保存在你的电脑上；它们只通过运行命令的文件作为
Kubernetes 机密进入群集，安装过程从不打印它们。之后应用只保留加密的主密钥，与连接的网关相同。

**群集由你维护。** 本应用只管理 LiteLLM 中的后端池、别名和密钥，不管理群集中的任何内容。网关面板列出了可直接复制的
命令，并链接到 Azure 门户中的群集：

```sh
az aks get-credentials -g <资源组> -n <名称>                  # kubectl 访问（Entra ID 登录）
kubectl -n litellm get pods,gateway,certificate              # LiteLLM、HTTPS 和证书
kubectl -n litellm get secret litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d
helm upgrade litellm oci://ghcr.io/berriai/litellm-helm -n litellm --reuse-values \
  --version <新版本> --set image.tag=v<新版本>                # 升级 LiteLLM
az aks get-upgrades -g <资源组> -n <名称> -o table && az aks upgrade -g <资源组> -n <名称> --kubernetes-version <版本>
```

以后想改用自己的域名：把域名指向该网关，修改 `litellm` 命名空间中的 `Certificate`、网关侦听器的 `hostname` 和
HTTPRoute，然后用新 URL 重新连接应用。**删除网关**与容器应用相同：删除群集（Azure 会一并删除其节点资源组）和其他带
标记的资源，从不删除资源组，且无法恢复。

## 部署到 Azure

这会把管理器本身托管在 App Service 上，使用 Entra 登录（Easy Auth），供团队管理一个网关：

- **APIM：** 一个已有的服务；如果你没有指定服务，部署会在其资源组中新建一个。
- **LiteLLM：** 一个**已有的**网关。Bicep 从不创建或更改它，因为它的机密由应用生成一次，重新预配不能轮换它们。
  所以请先用本地应用部署网关（[快速开始](#快速开始)第 1–3 步），或部署在 AKS 上。

前置条件：[azd](https://aka.ms/azd)、Azure CLI 以及 Node.js 20.19+ 或 22.12+。你需要在网关的资源组上，以及每个
扫描部署的订阅上，拥有创建自定义角色和角色分配的权限（所有者或用户访问管理员）。

**1. 创建用于登录的 Entra 应用注册**（只需一次）：

```sh
az ad app create --display-name "SteadyApiPool" \
  --sign-in-audience AzureADMyOrg --enable-id-token-issuance true
# 记下 appId。首次预配后再添加重定向 URI（第 3 步）。
```

**2. 为一种网关配置并预配：**

```sh
cd SteadyApiPool
azd env new pool-manager
azd env set ENTRA_CLIENT_ID <appId>
azd env set POOL_OWNERS "you@contoso.com"  # 可以进行更改的 UPN 或对象 ID，以逗号分隔
```

对于 **APIM**：

```sh
# 使用已有的 APIM 服务……
azd env set APIM_NAME <apim-name>
azd env set APIM_RESOURCE_GROUP <apim-resource-group>
# azd env set APIM_SUBSCRIPTION_ID <id>   # 仅当 APIM 位于其他订阅时
# ……或者不设置 APIM_NAME，在 azd 资源组中创建 apim-pool-<suffix>：
# azd env set APIM_SKU BasicV2            # BasicV2（默认）、StandardV2 或 Developer（30-40 分钟，无 SLA）
# azd env set APIM_PUBLISHER_EMAIL you@contoso.com   # 默认为 POOL_OWNERS 的第一项
```

对于 **LiteLLM**：

```sh
# 容器应用上的网关（它的容器应用名称）……
azd env set GATEWAY_NAME <gateway-name>
azd env set GATEWAY_RESOURCE_GROUP <gateway-resource-group>   # 默认为 azd 资源组
# azd env set GATEWAY_SUBSCRIPTION_ID <id>                     # 仅当它位于其他订阅时
# ……或其他地方的网关（AKS，任何带数据库的 LiteLLM 代理）：
# azd env set GATEWAY_URL https://litellm.contoso.com
# azd env set GATEWAY_MASTER_KEY sk-...
# azd env set GATEWAY_PRINCIPAL_ID <object-id>        # 可选：它调用 Foundry 时使用的标识
# azd env set GATEWAY_APP_INSIGHTS_ID <resource-id>   # 可选：它的 OpenTelemetry 发送到哪里
```

网关类型由你设置的内容决定：设置了 `GATEWAY_NAME`（或 `GATEWAY_URL`）表示 LiteLLM，否则为 APIM。
`azd env set GATEWAY_PRODUCT apim`（或 `litellm`）可以明确指定。然后：

```sh
azd provision --preview                    # 先查看 what-if 结果
azd up
```

**3. 注册输出中 `AUTH_REDIRECT_URI` 给出的重定向 URI：**

```sh
az ad app update --id <appId> --web-redirect-uris "$(azd env get-value AUTH_REDIRECT_URI)"
```

打开 `WEB_APP_URL` 并登录。`POOL_OWNERS` 中列出的用户可以进行更改并查看密钥。启用委托的 Azure 访问（见下文）后，
自身 Azure 角色已经允许的用户也可以：在 APIM 上，指应用所做的每项 APIM 写入操作；在 LiteLLM 上，指读取网关的
主密钥（容器应用机密），因为他们本来就能直接更改网关。对于不在容器应用上的 LiteLLM 网关，只有所有者可以。其他人
都是只读的。如果要限制谁能登录，请在应用的企业应用程序上设置*需要分配*。

### 委托的 Azure 访问（可选，推荐）

应用标识负责后端池操作，从不获得更改角色分配的权限。为了让设置向导能够**修复**访问问题（启用 APIM 的托管标识、
在 Foundry 上为网关的标识分配角色），并检查每个用户能做什么，Easy Auth 还会为登录用户获取一个 Azure 资源管理器
令牌。修复操作随后以该用户自己的 Azure 权限运行，并在活动日志中显示为该用户。如果不启用，向导仍会检查所有内容并
生成管理员申请，但 LiteLLM 的费用面板无法读取实际花费。

请先完成应用注册的设置。不完成这些步骤就打开此参数会导致无法登录：

```sh
APP_ID=<appId>
# 委托权限 "Azure Service Management / user_impersonation"，然后同意该权限。
az ad app permission add --id $APP_ID --api 797f4846-ba00-4fd7-ba43-dac1f8f63013 \
  --api-permissions 41094075-9dad-400e-a0bd-54e686782033=Scope
az ad app permission admin-consent --id $APP_ID   # 或让每个用户在首次登录时同意

azd env set ENABLE_DELEGATED_AZURE_ACCESS true
azd provision

# Easy Auth 使用托管标识（联合凭据）而不是客户端密码登录。
az ad app federated-credential create --id $APP_ID --parameters "{
  \"name\": \"pool-manager-easy-auth\",
  \"issuer\": \"$(azd env get-value AUTH_FIC_ISSUER)\",
  \"subject\": \"$(azd env get-value AUTH_FIC_SUBJECT)\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
```

在此之前已登录的用户需要重新登录；向导会为此显示**允许 Azure 访问**按钮。

### 部署的资源

| 资源 | 说明 |
| --- | --- |
| Linux App Service 计划（B1）+ Web 应用（Python 3.12） | 仅 HTTPS、TLS 1.2，禁用 FTP/SCM 基本身份验证，系统分配托管标识。设置了 `GATEWAY_URL` 时，LiteLLM 网关的主密钥保存为应用设置 |
| 用户分配托管标识 `id-pool-auth-*`（仅在启用委托的 Azure 访问时） | 作为 Easy Auth 的联合凭据，代替客户端密码。它没有任何 Azure 角色 |
| App Service 身份验证（Easy Auth） | 除 `/health` 外的每个路径都需要 Entra 登录 |
| APIM：API 管理服务 `apim-pool-*`（仅在未设置 `APIM_NAME` 时） | 默认为 `BasicV2`（`APIM_SKU`），带有供成员使用的系统分配托管标识，并标记 `managed-by: apim-backend-pool`。从创建起开始计费 |
| APIM：服务上的自定义角色 *APIM Backend Pool Manager* | 用于指标监控选项卡的 `Insights/metrics/read`、`backends/*`、`namedValues/*`、`apis/read,write,delete`（只在删除后端池时删除它自己创建的 API）、`apis/operations/write` 和 `apis/policies/read,write`，以及让设置检查应用自身访问权限的 `Authorization/permissions/read` |
| LiteLLM：网关的容器应用及其 Application Insights 组件上的自定义角色 *AI Gateway Pool Manager*（仅容器应用上的网关） | `Microsoft.App/containerApps/read` 和 `listSecrets/action`（网关的 URL、标识和主密钥；之后每次后端池更改都通过 LiteLLM 的管理 API 进行），用于指标监控选项卡的 `Insights/components/read` 和 `Insights/logs/*/read`，以及让设置检查应用自身访问权限的 `Authorization/permissions/read`。它不能更改容器应用、数据库或其他任何内容 |
| 每个发现订阅上的自定义角色 *APIM Backend Pool Deployment Reader* 或 *AI Gateway Pool Deployment Reader* | `CognitiveServices/accounts/read` 和 `accounts/deployments/read`，用于成员指标的 `Insights/metrics/read`，以及只读的 `Authorization/permissions`、`roleAssignments` 和 `roleDefinitions`，让设置可以检查网关在每个帐户上的角色。它没有 `listKeys`，也不能更改角色。用 Bicep 参数 `enableDeploymentDiscovery=false` 关闭，用 `discoverySubscriptionIds` 选择订阅（默认为网关所在订阅） |

网关自身的标识需要每个成员帐户上的 *Cognitive Services OpenAI User*（或任何允许推理的角色）。设置向导会检查这一点，
并在启用委托的 Azure 访问时授予。在 APIM 上，如果成员的机密引用 Key Vault，它的标识还需要该保管库上的
*Key Vault Secrets User* 角色。

### 配置

| 设置 | 默认值 | 用途 |
| --- | --- | --- |
| `POOL_MANAGER_SUBSCRIPTION_ID` / `_RESOURCE_GROUP` | 部署时必填 | 网关所在的位置。在本地可以不设置网关相关的设置，在设置页上选择、新建或连接网关 |
| `POOL_MANAGER_APIM_NAME` | 空 | APIM 服务，与上面两项一起设置。不要与 `_GATEWAY_NAME` 同时设置 |
| `POOL_MANAGER_GATEWAY_NAME` | 空 | LiteLLM 网关的容器应用，与上面两项一起设置；或与 `_GATEWAY_URL` 一起设置，作为其他地方的网关的显示名称 |
| `POOL_MANAGER_GATEWAY_URL` | 空 | 其他地方的 LiteLLM 网关（AKS，任何带数据库的 LiteLLM 代理）。此时 `_SUBSCRIPTION_ID` 只是默认搜索部署的订阅 |
| `POOL_MANAGER_GATEWAY_MASTER_KEY` | 空 | 该网关的主密钥。部署时必填；在本地也可以改由设置页加密保存 |
| `POOL_MANAGER_GATEWAY_PRINCIPAL_ID` | 空 | 该网关的托管标识（对象 ID），让设置可以检查并授予它对 Foundry 的访问权限 |
| `POOL_MANAGER_GATEWAY_APP_INSIGHTS_ID` | 空 | 它的 OpenTelemetry 发送到的 Application Insights 组件，用于指标监控选项卡 |
| `POOL_MANAGER_POOL_API_VERSION` | `2025-09-01-preview` | APIM：读写后端池使用的 API 版本。使用 `2024-05-01` 时会禁用会话感知，因为 GA 版本无法携带 `sessionAffinity` |
| `POOL_MANAGER_TENANT_ID` | 来自设置页 | 订阅所在的租户，本地令牌会向该租户请求 |
| `POOL_MANAGER_OWNERS` | 空 | 谁可以进行更改 |
| `POOL_MANAGER_RBAC_WRITERS` | `true` | 自身 Azure 角色允许的用户也可以进行更改（APIM：应用所做的每项写入操作；LiteLLM：读取主密钥）。需要委托的 Azure 访问 |
| `POOL_MANAGER_DELEGATED_ACCESS` | `false` | 由 `enableDelegatedAzureAccess` 设置：Easy Auth 提供用户的 ARM 令牌，让设置可以修复访问问题 |
| `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` | 网关所在订阅 | 扫描部署的订阅，以逗号分隔 |
| `POOL_MANAGER_PROTECTED_PREFIXES` | 空 | 以只读方式显示的 ID 前缀，以逗号分隔，用于由其他工具管理的资源（例如 `finops-`） |
| `POOL_MANAGER_ID_PREFIX` | `pool-` | 新后端池、成员和测试密钥的前缀 |
| `POOL_MANAGER_AUTH_MODE` | `easy_auth` | `disabled` 仅用于本地开发；在 App Service 上应用会拒绝以这种方式启动。`entra` 是[公开应用](#公开部署到-azure-容器应用)，仅支持 APIM |
| `POOL_MANAGER_SYSTEM_TRUST` | `true` | 在本地使用操作系统证书存储验证 TLS（适用于 TLS 检查背后的公司 CA） |
| `AZURE_CLIENT_ID` | — | 使用用户分配托管标识，而不是系统分配托管标识 |

## 公开部署到 Azure 容器应用

一个这样的部署运行在 https://ca-pool-public-5xgy5gsq3leyq.ambitiousground-f590833b.swedencentral.azurecontainerapps.io/
（选择**以访客身份浏览（只读）**无需登录即可浏览）。

`deploy/container-apps/` 把应用部署成一个公开网站：任何工作或学校帐户都可以用自己的 Microsoft Entra
身份登录，应用管理的是**他们自己的 Azure 角色**允许的 APIM 服务和 Foundry 部署。不需要预先为任何客户做配置，
应用自身也没有任何 Azure 访问权限。公开应用**只支持 APIM**：LiteLLM 网关需要把每个用户的主密钥放在服务器上，
而这种设计正是要避免这一点。

| | [部署到 Azure](#部署到-azure)（App Service） | 公开应用（容器应用） |
| --- | --- | --- |
| 谁可以登录 | 你自己租户的用户 | 任何工作或学校租户（或允许列表中的租户） |
| 网关 | 一个 APIM 服务或 LiteLLM 网关，部署时固定 | 每个用户选择或新建自己的 APIM 服务 |
| 谁调用 Azure | 应用的托管标识，使用自定义角色 | 登录用户本人，通过代表用户交换得到的令牌 |
| 谁可以进行更改 | `POOL_OWNERS`，或角色允许的用户 | 用户自己的 Azure 角色允许什么就能做什么 |

**工作方式。** 浏览器用 MSAL.js（多租户应用注册）登录，每个请求都带上一个用于此应用 API 的令牌。后端先验证它
（Entra 的签名密钥、受众、与令牌所属租户一致的颁发者、`access_as_user` 作用域），再代表用户把它交换成
Azure 资源管理器令牌。每一次 Azure 调用（APIM、Foundry、Azure Monitor、成本管理）都以该用户的身份运行，
因此由 Azure 决定是否允许，活动日志中记录的也是该用户。在这次交换中，应用用自己的用户分配托管标识证明身份，
它被配置为联合凭据，所以应用注册没有客户端密码。

**保存了什么。** 服务器上什么都不保存：没有密码、令牌、密钥或客户数据，也没有状态文件。Azure 令牌只存在于
用户的内存会话中，大约一小时后过期；用户退出登录（顶栏的**退出登录**）、30 分钟没有请求，或会话存储已满时
立即丢弃。在浏览器中，MSAL 把令牌保存在 `sessionStorage` 中（关闭标签页即消失），所选 APIM 服务的 ID
按帐户保存在 `localStorage` 中（退出登录时删除）。

**用户隔离。** 每个请求都单独验证身份，会话以验证过的租户 ID 和对象 ID 为键，绝不使用浏览器可以选择的值。
每个用户（以及他们打开的每个 APIM 服务）都有自己的一套组件，因此每份缓存（部署、指标、权限、费用）都只属于
一个用户；一个用户加载的数据不会用来响应另一个用户的请求，即使他们在同一个租户中。每个用户都有请求频率限制
（默认每分钟 600 个请求）。测试调用只会发往 Azure 自己的网关主机名（`*.azure-api.net`），后端不能被指向其他主机。

**部署。** 你需要 [azd](https://aka.ms/azd)、已登录（`az login`）到将拥有应用注册的租户的 Azure CLI、
Python 3，以及在目标资源组中创建应用注册和角色分配的权限（所有者，或参与者加用户访问管理员）。不需要 Docker
和 Node.js：映像在 Azure 容器注册表中构建。

```sh
cd deploy/container-apps
azd env new pool-public
# azd env set ALLOWED_TENANTS "<tenant-id>,<tenant-id>"   # 可选：只允许这些组织
azd up
```

`azd up` 会在预配前后运行 `scripts/public-app-registration.py`。预配前，它创建（或更新）多租户应用注册并设置
`ENTRA_CLIENT_ID`；预配后，它把容器应用的 `/redirect.html` 添加为重定向 URI，并为其托管标识添加联合凭据。
之后预配容器应用环境、注册表、标识和 Log Analytics 工作区，构建映像并部署。打开它输出的 `PUBLIC_APP_URL`。

**在其他组织使用之前：**
- 验证应用注册的发布者（*Microsoft Entra > 应用注册 > 品牌和属性*）。很多租户只允许用户同意来自已验证发布者的应用。
- 新组织的用户首次登录并同意此应用以其身份访问 *Azure 服务管理*时，应用会被安装到该租户（创建企业应用程序）；
  之后 Entra 可能还会就此应用自己的 API 再请求一次同意。如果用户不能自行同意（很多公司，包括 Microsoft 自己的租户，
  都要求管理员审批这类应用），该租户的管理员可以在
  `https://login.microsoftonline.com/<their-tenant-id>/adminconsent?client_id=<ENTRA_CLIENT_ID>`
  为所有人一次性同意，这会同时涵盖两项权限，之后也可以在*企业应用程序*中撤销。在此之前，该租户的登录会收到
  AADSTS500011（该租户中没有此应用）或需要管理员审批的提示。
- 和在门户中一样，用户仍需要对所管理的资源拥有 Azure 角色，例如 APIM 服务上的 *API Management Service
  Contributor*，以及 Foundry 资源上的*读者*。

**访客浏览（可选）。** 如果想让访客无需登录就能浏览，请在预配前指定一个 APIM 服务：

```sh
azd env set GUEST_APIM_NAME <apim-name>
azd env set GUEST_APIM_RESOURCE_GROUP <apim-resource-group>
# azd env set GUEST_APIM_SUBSCRIPTION_ID <id>   # 仅当它位于其他订阅时
azd provision && azd deploy
```

之后登录页会提供**以访客身份浏览（只读）**。访客可以看到该服务以及其所在订阅中的 Foundry 部署，读取时使用应用的托管标识；
它只在该订阅上获得内置的*读者*角色，不能做任何更改，也不能列出密钥和机密。后端还会拒绝访客的一切更改、订阅密钥、测试调用、
设置修复以及切换服务。所有访客共用一个缓存会话，并有频率限制（每个客户端地址每分钟 300 个请求，所有访客合计 3,000 个）。
该服务和订阅中的内容（例如后端池成员的 URL 和 API 策略）对任何拿到网址的人可见，所以请使用演示用的服务。

**限制。** 仅支持 Azure 公有云。一次登录对应一个租户；选择另一个租户中的服务时会登录到该租户。部署发现扫描所选
服务所在的订阅。网关使用自定义域的服务可以管理，但不能在应用中发送测试调用。

| 设置 | 默认值 | 用途 |
| --- | --- | --- |
| `POOL_MANAGER_AUTH_MODE` | `entra`（由模板设置） | 公开应用的登录方式 |
| `POOL_MANAGER_ENTRA_CLIENT_ID` | 来自钩子 | 多租户应用注册 |
| `POOL_MANAGER_FEDERATED_IDENTITY_CLIENT_ID` | 来自模板 | 用作应用联合凭据的托管标识 |
| `POOL_MANAGER_ENTRA_ALLOWED_TENANTS` | 空（`ALLOWED_TENANTS`） | 允许其用户登录的租户 ID；空表示任何工作或学校租户 |
| `POOL_MANAGER_SESSION_IDLE_SECONDS` | `1800` | 用户最后一次请求后经过这么久，会话（及其 Azure 令牌）结束 |
| `POOL_MANAGER_MAX_SESSIONS` | `500` | 每个副本保留的会话数；最久未使用的先移除 |
| `POOL_MANAGER_USER_REQUESTS_PER_MINUTE` | `600` | 每个用户每分钟的请求数 |

## 本地运行

在本地，应用运行在你的电脑上，并以**你**的身份操作 Azure。它只需要 **Python 3.11+**。只有开发浏览器界面时才需要
Node.js，Azure CLI 是可选的。

**最快的方式：发布包。** 从 [Releases](https://github.com/guming3d/SteadyApiPool/releases) 下载
`SteadyApiPool-<version>.zip`。它内置了浏览器界面和网关模板，因此不需要 Node.js 和 git。解压后运行：

```powershell
scripts\start-local.cmd                 # Windows；不受 PowerShell 执行策略影响
```

```sh
scripts/start-local.sh                  # macOS、Linux、WSL
```

打开 http://127.0.0.1:8000。设置页会先让你登录，然后让你选择网关类型和网关，或新建、连接网关（选择保存在
`backend/.pool-manager.json` 中，其中从不包含主密钥；连接的 LiteLLM 网关的密钥由操作系统加密保存在
`backend/.pool-manager-gateways.bin` 中）。如果要固定一个网关，请在 `backend/.env` 中设置
`POOL_MANAGER_SUBSCRIPTION_ID` 和 `_RESOURCE_GROUP`，再加上 `_APIM_NAME` 或 `_GATEWAY_NAME`（或者
`_GATEWAY_NAME` 和 `_GATEWAY_URL`）；`backend/.env.example` 给出了每种写法。

**在克隆的仓库中**，安装了 Node.js 22+ 时，同样的脚本会在 `:5174` 上运行 Vite 开发服务器。没有 Node.js 时，
如果界面已经构建过，它们会从 `backend/pool_manager/static` 提供界面（`-Prebuilt` / `--prebuilt` 强制如此）。

```powershell
.\scripts\start-local.ps1               # -Prebuilt -BackendPort 8001 -FrontendPort 5175 -SkipInstall -SkipChecks
```

```sh
scripts/start-local.sh                  # --prebuilt --backend-port 8001 --frontend-port 5175 --skip-install --skip-checks
```

脚本会先运行 `scripts/doctor.py --brief`。它只使用标准库，因此在安装任何东西之前、甚至在较旧的 Python 上也能运行，
并说明在当前操作系统上如何修复发现的问题。然后脚本只在依赖有变化时才安装，并一直运行到按下 Ctrl+C。
自己运行 `python scripts/doctor.py` 可以查看完整报告。

### 各操作系统的前置条件

| | Python 3.11+ | Node.js 22+（仅界面开发） | Azure CLI（可选） |
| --- | --- | --- | --- |
| Windows | `winget install -e --id Python.Python.3.13` | `winget install -e --id OpenJS.NodeJS.LTS` | `winget install -e --id Microsoft.AzureCLI` |
| macOS | `brew install python@3.13`（macOS 自带 3.9） | `brew install node` | `brew install azure-cli` |
| Ubuntu、Debian | `sudo apt install python3 python3-venv`；Ubuntu 22.04 / Debian 11 上为 `python3.11 python3.11-venv` | [nodejs.org](https://nodejs.org/en/download)（apt 中的版本较旧） | `curl -sL https://aka.ms/InstallAzureCLIDeb \| sudo bash` |
| Fedora、RHEL | `sudo dnf install python3.12` | [nodejs.org](https://nodejs.org/en/download) | [安装指南](https://learn.microsoft.com/cli/azure/install-azure-cli) |

在 Windows 上，如果 `python` 打开了 Microsoft Store，请在*设置 > 应用 > 高级应用设置 > 应用执行别名*中关闭该别名。
脚本会先查找 `python3.13` … `python3.11`，再查找 `python3`，因此能找到与系统 Python 并存的较新版本。

### 登录

应用按以下顺序尝试：在设置页上完成的登录，然后是 Azure CLI（`az login`）、Azure Developer CLI（`azd auth login`）
和 Azure PowerShell（`Connect-AzAccount`）。未安装或未登录的工具会被跳过一分钟，而不是在每个请求上重试。

- **用浏览器登录**会在本机打开 Microsoft 登录页面。
- **改用验证码登录**适用于没有浏览器的机器：SSH、Linux 服务器、容器，或浏览器无法打开的 WSL。在任意设备上打开
  https://microsoft.com/devicelogin 并输入显示的验证码。
- 在应用中完成的登录会在重启后**保留**，保存在操作系统的加密存储中（Windows 上为 DPAPI，macOS 上为钥匙串，
  Linux 上为 libsecret）。`backend/.pool-manager-auth.json` 只保存帐户记录。在无法加密的环境中（没有密钥环的 Linux 或
  WSL），应用会把登录状态和连接的网关的主密钥保存在内存中并给出提示；它从不以明文写入它们。**退出此登录**会清除登录。

### 网络代理与证书

- **代理：** 设置 `HTTPS_PROXY=http://proxy:8080`（以及 `NO_PROXY=localhost,127.0.0.1`）。应用、pip 和 npm 都会读取它。
- **TLS 检查**（公司证书）：把公司 CA 安装到操作系统证书存储中。在本地，应用会使用该存储验证 TLS
  （[truststore](https://pypi.org/project/truststore/)；`POOL_MANAGER_SYSTEM_TRUST=false` 可关闭）。pip 24.2+ 也是如此。
  对于较旧的 pip，设置 `PIP_CERT=<ca.pem>`；对于 npm，设置 `NODE_EXTRA_CA_CERTS=<ca.pem>`。
- **软件包镜像：** `PIP_INDEX_URL=https://<mirror>/simple/` 和 `npm_config_registry=https://<mirror>/`。doctor 会检查镜像，
  而不是 PyPI 和 npm。
- 应用需要访问 `https://login.microsoftonline.com` 和 `https://management.azure.com`，还需要访问你的网关 URL（测试
  调用，以及 LiteLLM 的管理 API）、用于 LiteLLM 网关指标监控选项卡的 `https://api.loganalytics.azure.com`，以及用于
  标价的 `https://prices.azure.com`。doctor 能区分 DNS 失败、超时、不受信任的证书以及要求凭据的代理。

### 远程机器和 WSL

通过 SSH 时，用验证码登录，并转发端口以在你自己的电脑上打开界面：`ssh -L 8000:localhost:8000 <host>`
（使用开发服务器时为 `5174`）。在 WSL 中，请在 WSL 内用 Linux 版 Python 运行 `scripts/start-local.sh`；
`.gitattributes` 会让 shell 脚本即使在 Windows 检出中也保持 LF 换行符。

### 手动运行

```sh
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/macOS 上为 bin/pip
cp .env.example .env                       # 无需修改；在设置页上选择网关
.venv/Scripts/python -m uvicorn pool_manager.app:app --reload           # 如果已构建，会提供 static/

cd ../frontend && npm ci && npm run dev    # http://localhost:5174，把 /api 代理到 :8000
```

在克隆的仓库中运行 `npm run build` 后，`python scripts/package-release.py` 会构建发布 zip。推送 `v*` 标签会发布一个
（`.github/workflows/release.yml`）。

修改 `infra/gateway/litellm-aca.bicep` 或 `litellm-aks.bicep` 后，请运行 `python scripts/build-gateway-template.py`
（需要带 Bicep 的 Azure CLI）重新编译 `backend/pool_manager/templates/litellm-{aca,aks}.json`，应用部署的就是这些
文件。CI 会用 `--check` 运行它。

> [!CAUTION]
> 本地开发使用**你**的 Azure 身份，作用于你**真实**的网关。所有更改立即生效。

## 测试

```sh
cd backend && .venv/Scripts/pip install pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
cd ../frontend && npm test && npm run build
```

后端测试使用一个内存中的 Azure（`tests/fake_azure.py`：ARM、模板部署、权限和角色、Foundry、Azure Monitor、
Log Analytics、成本管理和零售价格）、构建在它之上的内存中的 APIM（`tests/apim/fake_apim.py`，它会强制执行 ETag
以及 `If-Match` / `If-None-Match`），以及一个内存中的 LiteLLM 代理（`tests/litellm/fake_litellm.py`，它的管理 API
和聊天补全，按 v1.106 的行为建模）。不需要 Azure 访问。

另有一个可选的集成测试，让 LiteLLM 服务层在 Docker 中对接一个**真实**的 LiteLLM 代理，带有 PostgreSQL 和两个模拟的
Azure OpenAI 区域（`tests/litellm_stack/`）。它检查 LiteLLM 是否接受应用写入的行、凭据、别名和密钥，以及是否按
后端池的设置路由：

```sh
cd backend
docker compose -f tests/litellm_stack/docker-compose.yml up -d --wait
.venv/Scripts/python -m pytest -q -m litellm
```
