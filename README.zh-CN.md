# AI 网关后端池管理（LiteLLM）

[English](README.md) | **简体中文**

一个独立的 Web 应用，为你的 Azure OpenAI / Foundry 模型部署提供一个**带故障转移的 AI 网关**，无需先学习
LiteLLM 或 Foundry。它会部署（或连接）一个 [LiteLLM](https://github.com/BerriAI/litellm) 网关，找到你的模型部署，
并把同一模型的部署组合成一个**后端池**。客户端用一个模型名调用后端池；网关把请求分配给各个成员（故障转移、
轮询或加权），让持续失败的成员暂停一段时间（冷却），在另一个成员上重试，并让 Responses API 的后续请求回到最初
响应的部署。

![后端池的路由预览选项卡：客户端用一个模型名调用 LiteLLM 网关，后端池把请求分配到多个区域的 Foundry 部署，并带有冷却和重试](docs/images/pool-routing.png)

*客户端只用一个终结点和一个模型名，后面是多个模型部署：路由预览选项卡展示网关如何在后端池中分配流量；点击某个
部署，就能看到它故障或被限流时会发生什么。*

```
apim-backend-pool/
├── backend/    FastAPI 应用（pool_manager）及测试；同时提供构建好的前端
├── frontend/   React/Vite 界面（构建到 backend/pool_manager/static）
├── infra/      Bicep：容器应用上的网关（infra/gateway），以及用 Easy Auth 和最小权限角色
│               把本应用托管在 App Service 上
├── scripts/    本地启动脚本、doctor、演示服务器、发布打包和网关模板构建
├── docs/       本 README 的截图
├── tui/        终端界面：仍是 APIM 版本，暂不能用于网关
├── deploy/     公开的多租户部署：暂不支持网关
└── azure.yaml  azd 项目定义（App Service 托管）
```

> [!NOTE]
> 此分支取代了早先基于 Azure API Management（APIM）的版本。有两部分尚未移植：[终端界面](#终端界面)和
> [公开部署](#公开部署)。

第一次使用？[快速开始](#快速开始)大约需要半小时，其中大部分时间是等待 Azure 部署网关，只需要 Python。

## 快速开始

本指南在你自己的电脑上运行应用。你只需安装 Python，不需要 Azure CLI、Node.js、Docker、配置文件，也不需要
任何 LiteLLM 经验；应用会检查缺少什么，并和你一起补上。（如果想给团队一个共享的网址，请参阅
[部署到 Azure](#部署到-azure)。）

> [!NOTE]
> 截图为英文界面。浏览器语言为中文时界面显示中文，也可以用顶栏的语言按钮切换。下文使用中文界面上的按钮名称。

**你需要准备**

- **Python 3.11 或更高版本。** Windows 上运行 `winget install -e --id Python.Python.3.13`，
  macOS 上运行 `brew install python@3.13`，其他系统请参阅[各操作系统的前置条件](#各操作系统的前置条件)。
- **一个 Azure 帐户**：能在某个订阅中部署资源（*参与者*），或能读取一个已有的网关，并能读取你的
  Azure OpenAI / Foundry 资源（*读者*）。
- **一些 Azure OpenAI / Foundry 模型部署**，最好是同一个模型部署在多个区域。如果还没有，
  应用可以帮你创建（第 4 步）。

### 1. 下载并启动

从 [Releases](https://github.com/guming3d/apim-backend-pool-manager/releases) 下载
`apim-backend-pool-<version>.zip`，解压后在解压出的文件夹中运行启动脚本：

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

### 3. 选择、部署或连接网关

应用会列出你能访问的每个订阅中运行在 Azure 容器应用上的 LiteLLM 网关，并说明你是否有权管理它们。点击
**使用此网关**。

![设置页第 2 步：订阅中的 LiteLLM 网关，每个都有“使用此网关”按钮，另有“部署网关”和“连接已有网关”](docs/images/onboarding-2-choose-gateway.png)

还没有网关？点击**部署网关**。应用会先问**网关在哪里运行？**（见下文[容器应用还是 AKS？](#容器应用还是-aks)），
并给出推荐：

- **部署到容器应用**会打开一个所有内容都已填好的表单：唯一的名称、资源组、区域和规模（**开发 / 试用**或
  **生产**），并附有标价估算。点击**部署**。应用以你自己的 Azure 权限部署 LiteLLM、PostgreSQL、一个托管标识和
  Application Insights，并跟踪部署进度，通常需要 5–15 分钟（PostgreSQL 最慢）。期间可以关闭应用，设置会接着跟踪。
  资源从创建起就开始计费。
- **在 AKS 上部署**为**新的 AKS 群集**打开同样的表单。点击**部署**后，应用分三个阶段进行，并实时显示进度（通常共
  15–25 分钟）：
  1. **Azure 资源**：群集（Microsoft Entra ID 与 Azure RBAC、工作负载标识、托管 Gateway API）、PostgreSQL、网关
     的托管标识、Application Insights，以及生产配置的 Redis。你会获得该群集的“Azure Kubernetes 服务 RBAC 群集管理员”
     角色，因此部署需要资源组上的所有者或用户访问管理员权限。
  2. **在群集中安装**：通过 AKS 运行命令（你的电脑上无需 kubectl 或 Helm）安装 cert-manager、OpenTelemetry
     Collector、LiteLLM 的 Helm 图表和 HTTPS 网关。
  3. **HTTPS 证书**：Let's Encrypt 为 `https://<名称>.<区域>.cloudapp.azure.com` 签发证书；LiteLLM 在该地址上
     应答后，应用就像连接任何网关一样连接它。

  运行期间请保持此页打开（进度会推动安装继续；关闭后回到“AI 网关”这一步即可继续）。安装失败时会显示其输出，并提供
  **重新运行安装**。此后群集**由你维护**，见[AKS 上的网关](#aks-上的网关)。
- **使用已有群集（生成文件）**适用于你已有的群集：它会询问群集、其资源组和命名空间，然后生成 `values.yaml`、
  `deploy.sh` 和 `README.md`（Helm 图表、工作负载标识、PostgreSQL 灵活服务器）。群集需要启用 OIDC 颁发者和工作负载
  标识。运行该脚本（例如在 Azure Cloud Shell 中），然后点击**已部署，去连接**。脚本通过公共 IP 以纯 HTTP 公开
  LiteLLM，而**连接**只接受 HTTPS URL（纯 HTTP 仅限 localhost，主密钥不会以明文经过网络）。想在运行本应用的电脑上
  先试用，可保持 `kubectl -n <命名空间> port-forward svc/litellm 4000:4000` 运行并连接 `http://localhost:4000`；
  正式使用时，请在它前面加上 HTTPS，并连接其 HTTPS URL。

**连接已有网关**适用于任何带数据库（`STORE_MODEL_IN_DB`）的 LiteLLM 代理：输入它的 HTTPS URL、主密钥，以及可选的
它调用 Foundry 时使用的托管标识对象 ID 和它的 Application Insights 资源 ID。应用会用网关本身验证 URL 和主密钥，
并把主密钥加密保存在本机，从不以明文写入文件。

![网关在哪里运行？几个问题，推荐 Azure 容器应用，以及 AKS 选项](docs/images/onboarding-2b-deploy-gateway.png)

接下来会打开一个两分钟的导览。可以跟着看，也可以跳过；顶栏的**导览**可以重新播放。

#### 容器应用还是 AKS？

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

后端池打开时所有内容都已填好：ID（这里是 `pool-gpt-5-6-luna`，它也是客户端发送的模型名）、成员，以及冷却、
重试和超时设置。默认值即可使用。**故障转移**把所有流量发给主部署，在它失败或配额用尽时切换到备用部署；如果想把
流量分摊到所有成员，请选择**轮询**，或选择**加权**自行设置比例。

![添加后端池，已预填故障转移模式下的三个成员和冷却设置](docs/images/onboarding-4-create-pool.png)

点击**发布到网关**。应用会准确列出它将写入的内容。再次点击**发布到网关**确认。更改会立即在网关上生效。

![确认发布：新的后端池及其三个成员](docs/images/onboarding-5-publish.png)

### 6. 允许网关调用你的模型，并获取密钥

后端池会打开到**调用测试**选项卡。它显示网关的 URL 和后端池的模型名（**添加别名**可以让已经发送其他名称
（例如 `gpt-5.6-luna`）的客户端也调用到此后端池），并带你完成剩下的步骤：

1. **全部授予**为网关的托管标识在每个 Foundry 资源上分配 *Cognitive Services OpenAI User* 角色，这样网关无需
   API 密钥即可调用你的模型。这需要一个能在这些资源上分配角色的角色（*所有者*、*用户访问管理员*或*基于角色的
   访问控制管理员*）。如果没有，顶栏的**权限检查**会给出一份可以直接发给 Azure 管理员的申请。
2. **创建测试密钥**会创建一个只能调用此后端池模型名的 LiteLLM 虚拟密钥，并自动填入。LiteLLM 只保存它的哈希，
   所以如果想保留，请现在复制：它只显示这一次。

![调用测试选项卡：终结点与模型名、调用前准备，以及获取虚拟密钥和“创建测试密钥”](docs/images/onboarding-6-grant-and-key.png)

### 7. 发送测试请求

点击**发送请求**。请求会经过网关和后端池，你会得到模型的回答以及处理请求的成员（还有 LiteLLM 之前在其他成员上
尝试了几次）。如果刚刚授予访问权限，Azure 最多可能需要 10 分钟才能使角色生效；在此之前页面会解释错误原因，
请稍后重试。

![在这里测试：200，调用成功，由后端池的主成员 pool-aoai-westus3-gpt-5-6-luna-dz 响应](docs/images/onboarding-7-test-call.png)

你的应用像调用任何 OpenAI 终结点一样调用后端池：基础 URL 为 `<gateway>/v1`，虚拟密钥作为 API 密钥，后端池 ID
（或别名）作为模型名，不需要 `api-version`。页面下方的**发送请求**部分提供 curl、Python、JavaScript 和
PowerShell 代码，并已填好你的 URL。Python 示例：

```python
from openai import OpenAI

client = OpenAI(base_url="https://<your-gateway>/v1", api_key="<your-virtual-key>")

response = client.chat.completions.create(
    model="pool-gpt-5-6-luna",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

### 8. 观察运行情况

**指标监控**选项卡显示请求数、可用性、返回给客户端的 429（以及故障转移挽回了多少）和延迟，以及每个成员承担的
流量份额和令牌数。**路由预览**可以模拟成员故障，让你在真正发生之前看到故障转移的过程。

![指标监控选项卡：请求数、可用性、429、延迟和每个成员的流量份额](docs/images/onboarding-8-metrics.png)

就这些。其他功能（更多后端池、权重、别名、自定义终结点、LiteLLM 配置视图、清理）都在同一个界面中；
[功能一览](#功能一览)列出了全部功能。

> [!CAUTION]
> 应用以**你**的身份操作 Azure，并用主密钥操作你**真实**的网关。所有更改对所有客户端立即生效。

**如果遇到问题**

- 顶栏的**权限检查**会重新运行设置检查。凡是你自己的 Azure 权限无法修复的问题，都会变成一份发给管理员的申请：
  角色、作用域、一条 `az` 命令和一个门户链接。
- `python scripts/doctor.py` 会说明你的电脑或网络有什么问题（Python 版本、代理、公司 TLS 证书、DNS），
  以及在你的操作系统上如何修复。
- 在代理后面或受限网络中？请参阅[网络代理与证书](#网络代理与证书)。

**不用 Azure 试用。** 在克隆的仓库中，`scripts/demo_server.py` 会让同一个界面连接一个内存中的 Azure 和 LiteLLM
网关，带有示例部署，上面的截图就是这样制作的。它不会访问 Azure。它需要后端的虚拟环境（启动脚本会创建）和构建好的
界面（Node.js 22+）：

```sh
cd frontend && npm ci && npm run build && cd ..
backend/.venv/Scripts/python scripts/demo_server.py    # macOS 和 Linux 上为 bin/python；http://127.0.0.1:8010
```

它像首次运行一样从未登录状态开始。`POST /demo/stage/{sign-in,ready,pool}` 可以跳到引导流程中的某个阶段
（`pool` 是一个已在使用、带有一小时指标的后端池）。

## 功能一览

| 功能 | 说明 |
| --- | --- |
| **设置向导** | 四个步骤：**登录**、**AI 网关**、**权限**和**网关 → Foundry**。它检查登录、网关（可访问、数据库已连接、主密钥有效、托管标识）、你对网关的访问权限，以及网关对 Foundry 的访问权限。能用你的 Azure 权限修复的就直接修复，其余的生成一份可以直接发给管理员的申请。在本地还可以选择、部署、连接、断开或删除网关。 |
| **网关选择** | 根据几个回答推荐容器应用或 AKS。可一步部署到容器应用，或新建 AKS 群集并在其上安装带 HTTPS 的 LiteLLM，或为已有群集生成部署文件。 |
| **导览** | 面向 AI 网关新手的两分钟页面导览。设置完成后自动运行一次；顶栏的**导览**可以重新播放。 |
| **部署发现** | 按模型分组，列出你的订阅中的 Azure OpenAI / Foundry 部署，并把选中的部署导入到新的或已有的后端池。 |
| **部署模型** | 用你自己的 Azure 权限在 Foundry 帐户中创建按令牌计费的 OpenAI 模型部署，无需打开 Foundry 门户。 |
| **后端池** | 创建和编辑包含 2–30 个成员的后端池，支持**故障转移**、**轮询**或**加权**模式。后端池是一个 LiteLLM 模型组，它的 ID 就是客户端发送的模型名。 |
| **成员** | 在所属后端池中编辑。除了 Foundry 部署，**添加自定义终结点**还可以添加另一个 Azure OpenAI 资源或任何兼容 OpenAI 的 API，身份验证可以是无、API 密钥（由 LiteLLM 作为加密凭据保存）或网关的托管标识。 |
| **冷却、重试、超时** | 后端池设置，写入每个成员：一分钟内失败多少次会让成员暂停、暂停多久，失败的请求在另一个成员上重试几次，以及单次尝试的超时。 |
| **模型名和别名** | 后端池 ID 始终可用；别名把其他模型名指向此后端池，已有的客户端无需修改。 |
| **LiteLLM 配置** | **查看 LiteLLM 配置**以 LiteLLM `config.yaml` 的写法显示后端池（不含机密），发布前可查看差异，也可以**导出 YAML**。 |
| **路由预览** | 显示网关如何通过后端池路由流量，并模拟故障、冷却和恢复。 |
| **调用测试** | 创建后端池的测试虚拟密钥，在页面上发送测试请求并显示由哪个成员响应，还提供可直接复制的客户端代码。 |
| **指标监控** | 后端池的请求数、可用性、429 和延迟来自网关在 Application Insights 中的 OpenTelemetry 跟踪；每个成员的流量份额、令牌数和延迟来自 Azure Monitor。 |
| **费用** | 网关资源本月的花费和标价。点击顶栏中的网关名称（**管理网关**）打开，那里还有 LiteLLM 自带管理界面的链接。 |
| **同一模型** | 如果后端池中的 Foundry 成员提供不同的模型，则拒绝保存。模型从 Azure 读取，而不是根据部署名称猜测。 |
| **删除** | 删除一个后端池（连同它的别名和测试密钥），或此应用在网关上创建的全部内容，删除前会准确列出将被删除的内容。在本地还可以删除此应用部署的网关。 |
| **安全性** | 应用只更改和删除它自己创建的内容，并且只在你确认过之后才删除。在其他地方（LiteLLM 管理界面或配置）添加的后端池以只读方式显示，带有受保护 ID 前缀（`POOL_MANAGER_PROTECTED_PREFIXES`）的后端池也是如此。 |

新后端池的默认值：
- **冷却：** 成员在一分钟内失败一次（429 也算）就暂停 60 秒；它的流量转给其他成员，故障转移模式下转给下一个优先级。
- **重试：** 在另一个成员上再尝试 1 次。
- **单次尝试超时：** 120 秒。

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
- **成员：** 每个导入的成员指向 `https://<account>.openai.azure.com/openai/deployments/<deployment>`，并成为 LiteLLM 中的一行 `azure/<deployment>`。因此同一模型名称不同的部署，例如 `gpt-5.6-luna` 和 DataZone 的 `gpt-5.6-luna-dz`，可以放进同一个后端池。
- **客户端如何调用：** `https://<gateway>/v1/chat/completions`（或 `/v1/responses`），带上 `Authorization: Bearer <虚拟密钥>`，`model` 为后端池 ID 或别名。
- **身份验证：** 成员使用**网关的托管标识**进行身份验证，因为大多数帐户禁用了密钥身份验证。它需要每个帐户上的 **Cognitive Services OpenAI User** 角色；设置向导、模型部署选择栏和调用测试选项卡会检查这一点，并在你有权限时授予（**授予访问权限**），否则给出一份带有完整 `az role assignment create` 命令的管理员申请。
- **缓存：** 结果缓存 5 分钟。**刷新**会重新扫描，25 个帐户大约需要 15 秒。

### 容器应用上的网关

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

### AKS 上的网关

**在 AKS 上部署**会创建以下内容（`infra/gateway/litellm-aks.bicep`，编译为
`backend/pool_manager/templates/litellm-aks.json`，随后是 `gateway_template.aks_bootstrap` 中的安装）。Azure
资源的标记与容器应用网关相同。

| 资源 | 说明 |
| --- | --- |
| AKS 群集 `<名称>` | Microsoft Entra ID 与 Azure RBAC（无本地帐户），OIDC 颁发者和工作负载标识，Azure CNI 覆盖网络，Azure Linux 节点（Standard_D4ds_v5）。入口使用托管 Gateway API 和应用程序路由的 Istio 实现（托管 NGINX 入口已停用）。补丁版本和节点映像自动升级 |
| 群集中的 `litellm` 命名空间 | LiteLLM 的 Helm 图表（固定为 `1.106.4`，使用工作负载标识）、把模型调用跟踪发送到 Application Insights 的 OpenTelemetry Collector，以及位于公共 IP 上、DNS 名称为 `<名称>.<区域>.cloudapp.azure.com` 的网关。cert-manager（位于 `cert-manager`）获取并续订 Let's Encrypt 证书。纯 HTTP 只会重定向到 HTTPS |
| 用户分配的托管标识 `id-<名称>` | 与 LiteLLM 的服务帐户联合；LiteLLM 以它的身份调用 Foundry。设置向导会在成员帐户上为它授予“认知服务 OpenAI 用户” |
| PostgreSQL、Redis（生产）、Log Analytics、Application Insights | 与容器应用相同 |
| 角色分配 | 你在该群集上的“Azure Kubernetes 服务 RBAC 群集管理员” |

机密（主密钥、盐密钥、Postgres 密码）由应用生成，安装完成前保存在你的电脑上；它们只通过运行命令的文件作为
Kubernetes 机密进入群集，安装过程从不打印它们。之后应用只保留加密的主密钥，与连接的网关相同。

**群集由你维护。**本应用只管理 LiteLLM 中的后端池、别名和密钥，不管理群集中的任何内容。网关面板列出了可直接复制的
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

这会把管理器本身托管在 App Service 上，使用 Entra 登录（Easy Auth），供团队使用。它管理一个**已有的**网关：
Bicep 从不创建或更改网关，因为网关的机密由应用生成一次，重新预配不能轮换它们。所以请先用本地应用部署网关
（[快速开始](#快速开始)第 1–3 步），或部署在 AKS 上。

前置条件：[azd](https://aka.ms/azd)、Azure CLI 以及 Node.js 20.19+ 或 22.12+。你需要在网关的资源组上，以及每个
扫描部署的订阅上，拥有创建自定义角色和角色分配的权限（所有者或用户访问管理员）。

**1. 创建用于登录的 Entra 应用注册**（只需一次）：

```sh
az ad app create --display-name "AI Gateway Pool Manager" \
  --sign-in-audience AzureADMyOrg --enable-id-token-issuance true
# 记下 appId。首次预配后再添加重定向 URI（第 3 步）。
```

**2. 配置并预配：**

```sh
cd apim-backend-pool
azd env new pool-manager
# 容器应用上的网关（它的容器应用名称）……
azd env set GATEWAY_NAME <gateway-name>
azd env set GATEWAY_RESOURCE_GROUP <gateway-resource-group>   # 默认为 azd 资源组
# azd env set GATEWAY_SUBSCRIPTION_ID <id>                     # 仅当它位于其他订阅时
# ……或其他地方的网关（AKS，任何带数据库的 LiteLLM 代理）：
# azd env set GATEWAY_URL https://litellm.contoso.com
# azd env set GATEWAY_MASTER_KEY sk-...
# azd env set GATEWAY_PRINCIPAL_ID <object-id>        # 可选：它调用 Foundry 时使用的标识
# azd env set GATEWAY_APP_INSIGHTS_ID <resource-id>   # 可选：它的 OpenTelemetry 发送到哪里
azd env set ENTRA_CLIENT_ID <appId>
azd env set POOL_OWNERS "you@contoso.com"  # 可以进行更改的 UPN 或对象 ID，以逗号分隔
azd provision --preview                    # 先查看 what-if 结果
azd up
```

**3. 注册输出中 `AUTH_REDIRECT_URI` 给出的重定向 URI：**

```sh
az ad app update --id <appId> --web-redirect-uris "$(azd env get-value AUTH_REDIRECT_URI)"
```

打开 `WEB_APP_URL` 并登录。`POOL_OWNERS` 中列出的用户可以进行更改并查看密钥。启用委托的 Azure 访问（见下文）后，
自身 Azure 角色已经允许读取网关主密钥（容器应用机密）的用户也可以，因为他们本来就能直接更改网关。对于不在容器
应用上的网关，只有所有者可以。其他人都是只读的。如果要限制谁能登录，请在应用的企业应用程序上设置*需要分配*。

### 委托的 Azure 访问（可选，推荐）

应用标识负责读取网关，从不获得更改角色分配的权限。为了让设置向导能够**修复**访问问题（在 Foundry 上为网关的标识
分配角色），并检查每个用户能做什么，Easy Auth 还会为登录用户获取一个 Azure 资源管理器令牌。修复操作随后以该用户
自己的 Azure 权限运行，并在活动日志中显示为该用户。如果不启用，向导仍会检查所有内容并生成管理员申请，但费用面板
无法读取实际花费。

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
| Linux App Service 计划（B1）+ Web 应用（Python 3.12） | 仅 HTTPS、TLS 1.2，禁用 FTP/SCM 基本身份验证，系统分配托管标识。设置了 `GATEWAY_URL` 时，网关的主密钥保存为应用设置 |
| 用户分配托管标识 `id-pool-auth-*`（仅在启用委托的 Azure 访问时） | 作为 Easy Auth 的联合凭据，代替客户端密码。它没有任何 Azure 角色 |
| App Service 身份验证（Easy Auth） | 除 `/health` 外的每个路径都需要 Entra 登录 |
| 网关的容器应用及其 Application Insights 组件上的自定义角色 *AI Gateway Pool Manager*（仅容器应用上的网关） | `Microsoft.App/containerApps/read` 和 `listSecrets/action`（网关的 URL、标识和主密钥；之后每次后端池更改都通过 LiteLLM 的管理 API 进行），用于指标监控选项卡的 `Insights/components/read` 和 `Insights/logs/*/read`，以及让设置检查应用自身访问权限的 `Authorization/permissions/read`。它不能更改容器应用、数据库或其他任何内容 |
| 每个发现订阅上的自定义角色 *AI Gateway Pool Deployment Reader* | `CognitiveServices/accounts/read` 和 `accounts/deployments/read`，用于成员指标的 `Insights/metrics/read`，以及只读的 `Authorization/permissions`、`roleAssignments` 和 `roleDefinitions`，让设置可以检查网关在每个帐户上的角色。它没有 `listKeys`，也不能更改角色。用 Bicep 参数 `enableDeploymentDiscovery=false` 关闭，用 `discoverySubscriptionIds` 选择订阅（默认为网关所在订阅） |

网关自身的标识需要每个成员帐户上的 *Cognitive Services OpenAI User*（或任何允许推理的角色）。设置向导会检查这一点，
并在启用委托的 Azure 访问时授予。

### 配置

| 设置 | 默认值 | 用途 |
| --- | --- | --- |
| `POOL_MANAGER_SUBSCRIPTION_ID` / `_RESOURCE_GROUP` / `_GATEWAY_NAME` | 部署时必填（或 `_GATEWAY_URL`） | 容器应用上的网关。在本地可以不设置，在设置页上选择、部署或连接网关；三项都设置则固定为该网关 |
| `POOL_MANAGER_GATEWAY_URL` | 空 | 其他地方的网关（AKS，任何带数据库的 LiteLLM 代理），`_GATEWAY_NAME` 作为显示名称。此时 `_SUBSCRIPTION_ID` 只是默认搜索部署的订阅 |
| `POOL_MANAGER_GATEWAY_MASTER_KEY` | 空 | 该网关的主密钥。部署时必填；在本地也可以改由设置页加密保存 |
| `POOL_MANAGER_GATEWAY_PRINCIPAL_ID` | 空 | 该网关的托管标识（对象 ID），让设置可以检查并授予它对 Foundry 的访问权限 |
| `POOL_MANAGER_GATEWAY_APP_INSIGHTS_ID` | 空 | 它的 OpenTelemetry 发送到的 Application Insights 组件，用于指标监控选项卡 |
| `POOL_MANAGER_TENANT_ID` | 来自设置页 | 订阅所在的租户，本地令牌会向该租户请求 |
| `POOL_MANAGER_OWNERS` | 空 | 谁可以进行更改 |
| `POOL_MANAGER_RBAC_WRITERS` | `true` | 自身 Azure 角色允许读取网关主密钥的用户也可以进行更改。需要委托的 Azure 访问 |
| `POOL_MANAGER_DELEGATED_ACCESS` | `false` | 由 `enableDelegatedAzureAccess` 设置：Easy Auth 提供用户的 ARM 令牌，让设置可以修复访问问题 |
| `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` | 网关所在订阅 | 扫描部署的订阅，以逗号分隔 |
| `POOL_MANAGER_PROTECTED_PREFIXES` | 空 | 以只读方式显示的后端池 ID 前缀，以逗号分隔，用于由其他工具管理的后端池（例如 `finops-`） |
| `POOL_MANAGER_ID_PREFIX` | `pool-` | 新后端池、成员和测试密钥的前缀 |
| `POOL_MANAGER_AUTH_MODE` | `easy_auth` | `disabled` 仅用于本地开发；在 App Service 上应用会拒绝以这种方式启动。`entra`（公开部署）暂不支持网关 |
| `POOL_MANAGER_SYSTEM_TRUST` | `true` | 在本地使用操作系统证书存储验证 TLS（适用于 TLS 检查背后的公司 CA） |
| `AZURE_CLIENT_ID` | — | 使用用户分配托管标识，而不是系统分配托管标识 |

## 公开部署

部署在容器应用上、任何人都可以用自己的 Microsoft 帐户登录的公开多租户网站（`deploy/container-apps/`，
`POOL_MANAGER_AUTH_MODE=entra`）**暂不支持 LiteLLM 网关**：每个用户需要带上自己的网关和主密钥，这部分尚未实现。
后端会拒绝以这种模式启动。请在本地使用应用，或为团队[部署到 App Service](#部署到-azure)。

## 本地运行

在本地，应用运行在你的电脑上，并以**你**的身份操作 Azure。它只需要 **Python 3.11+**。只有开发浏览器界面时才需要
Node.js，Azure CLI 是可选的。

**最快的方式：发布包。** 从 [Releases](https://github.com/guming3d/apim-backend-pool-manager/releases) 下载
`apim-backend-pool-<version>.zip`。它内置了浏览器界面和网关模板，因此不需要 Node.js 和 git。解压后运行：

```powershell
scripts\start-local.cmd                 # Windows；不受 PowerShell 执行策略影响
```

```sh
scripts/start-local.sh                  # macOS、Linux、WSL
```

打开 http://127.0.0.1:8000。设置页会先让你登录，然后让你选择、部署或连接网关（选择保存在
`backend/.pool-manager.json` 中，其中从不包含主密钥；连接的网关的密钥由操作系统加密保存在
`backend/.pool-manager-gateways.bin` 中）。如果要固定一个网关，请在 `backend/.env` 中设置
`POOL_MANAGER_SUBSCRIPTION_ID`、`_RESOURCE_GROUP` 和 `_GATEWAY_NAME`（或 `_GATEWAY_NAME` 和 `_GATEWAY_URL`）；
`backend/.env.example` 给出了两种写法。

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
- 应用需要访问 `https://login.microsoftonline.com` 和 `https://management.azure.com`，还需要访问你的网关 URL（它的
  管理 API 和测试调用）、用于指标监控选项卡的 `https://api.loganalytics.azure.com`，以及用于标价的
  `https://prices.azure.com`。doctor 能区分 DNS 失败、超时、不受信任的证书以及要求凭据的代理。

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

修改 `infra/gateway/litellm-aca.bicep` 后，请运行 `python scripts/build-gateway-template.py`（需要带 Bicep 的
Azure CLI）重新编译 `backend/pool_manager/templates/litellm-aca.json`，应用部署的就是这个文件。CI 会用 `--check`
运行它。

### 终端界面

`tui/` 是后端 `/api` 的 [Textual](https://textual.textualize.io/) 终端客户端。它仍然针对 APIM 版本，尚未移植到
LiteLLM 网关，所以 `start-local.ps1 -Tui` / `start-local.sh --tui` 会给出提示并拒绝启动。它不在 CI 中检查，
也不包含在发布包中。

> [!CAUTION]
> 本地开发使用**你**的 Azure 身份，作用于你**真实**的网关。所有更改立即生效。

## 测试

```sh
cd backend && .venv/Scripts/pip install pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
cd ../frontend && npm test && npm run build
```

后端测试使用一个内存中的 Azure（`tests/fake_azure.py`：ARM、网关的容器应用及其机密、模板部署、权限和角色、
Foundry、Azure Monitor、Log Analytics、成本管理和零售价格）和一个内存中的 LiteLLM 代理（`tests/fake_litellm.py`，
它的管理 API 和聊天补全，按 v1.106 的行为建模）。不需要 Azure 访问。

另有一个可选的集成测试，让服务层在 Docker 中对接一个**真实**的 LiteLLM 代理，带有 PostgreSQL 和两个模拟的
Azure OpenAI 区域（`tests/litellm_stack/`）。它检查 LiteLLM 是否接受应用写入的行、凭据、别名和密钥，以及是否按
后端池的设置路由：

```sh
cd backend
docker compose -f tests/litellm_stack/docker-compose.yml up -d --wait
.venv/Scripts/python -m pytest -q -m litellm
```
