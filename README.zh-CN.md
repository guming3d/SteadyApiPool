# APIM Backend Pool Manager

[English](README.md) | **简体中文**

一个独立的 Web 应用，用于管理 Azure API Management 的**原生后端池**：故障转移、轮询和加权分配、
断路器以及会话感知。它还能把后端池绑定到 API。

```
apim-backend-pool/
├── backend/    FastAPI 应用（pool_manager）及测试；同时提供构建好的前端
├── frontend/   React/Vite 界面（构建到 backend/pool_manager/static）
├── tui/        终端界面（Textual），后端 /api 的另一个客户端
├── infra/      Bicep：App Service + Easy Auth + 最小权限的 APIM 角色
├── deploy/     Container Apps 上的公开部署（独立的 azd 项目）
├── scripts/    本地启动脚本（浏览器或 -Tui）、演示服务器和 azd 前端构建钩子
├── docs/       本 README 的截图
└── azure.yaml  azd 项目定义
```

**在线演示：** https://ca-pool-public-5xgy5gsq3leyq.ambitiousground-f590833b.swedencentral.azurecontainerapps.io/（选择**以访客身份浏览（只读）**无需登录即可浏览，
也可以登录后管理你自己的 APIM 服务）。

第一次使用？[快速开始](#快速开始)大约需要十分钟，只需要 Python。如果想把它作为网站提供给其他组织，
让每个人用自己的 Azure 帐户登录，请参阅[公开部署到 Azure Container Apps](#公开部署到-azure-container-apps)。

## 快速开始

本指南在你自己的电脑上运行应用。你只需安装 Python，不需要 Azure CLI、Node.js、配置文件，也不需要
APIM 经验；应用会检查缺少什么，并和你一起补上。（如果想给团队一个共享的网址，请参阅
[部署到 Azure](#部署到-azure)。）

> [!NOTE]
> 截图为英文界面。浏览器语言为中文时界面显示中文，也可以用顶栏的语言按钮切换。下文使用中文界面上的按钮名称。

**你需要准备**

- **Python 3.11 或更高版本。** Windows 上运行 `winget install -e --id Python.Python.3.13`，
  macOS 上运行 `brew install python@3.13`，其他系统请参阅[各操作系统的前置条件](#各操作系统的前置条件)。
- **一个 Azure 帐户**：能够管理一个 API Management 服务，或能在某个订阅中创建服务（*参与者*），
  并能读取你的 Azure OpenAI / Foundry 资源（*读者*）。
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

![设置页第 1 步：用浏览器登录](docs/images/onboarding-1-sign-in.png)

### 3. 选择 APIM 服务，或新建一个

应用会列出你能访问的每个订阅中的 API Management 服务，并说明你是否有权管理它们。点击**使用此服务**。

![设置页第 2 步：订阅中的 APIM 服务，每个都有“使用此服务”按钮](docs/images/onboarding-2-choose-apim.png)

还没有 APIM 服务？点击**新建 APIM 服务**。所有内容都已填好：唯一的名称、新的资源组、区域，以及推荐的
**基本 v2** 层。确认发布者电子邮件后点击**创建并使用**。应用会等到服务就绪，通常需要几分钟
（开发人员层需要 30–40 分钟）。服务从创建起就开始计费。

![新建 APIM 服务表单，已预填基本 v2 层](docs/images/onboarding-2b-create-apim.png)

接下来会打开一个两分钟的导览。可以跟着看，也可以跳过；顶栏的**导览**可以重新播放。

### 4. 选择要放进后端池的部署

打开**模型部署**选项卡。它按模型分组，列出 APIM 服务所在订阅中的每个 Azure OpenAI / Foundry 部署。
（其他订阅中的部署：在 `backend/.env` 中设置 `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS`。）在你想要的模型上点击
**用此模型创建后端池**。如果只想放入某个模型的部分部署，勾选它们后选择**导入并创建后端池**。

![模型部署选项卡：gpt-5.6-luna 部署在三个区域，带有“用此模型创建后端池”按钮](docs/images/onboarding-3-deployments.png)

还没有任何部署？**部署模型**会在你的某个 Foundry 资源中创建部署，然后在这里列出。

### 5. 发布后端池

后端池打开时所有内容都已填好：ID、客户端将调用的 API 路径、成员和断路器。默认值即可使用。
**故障转移**把所有流量发给主部署，在它失败或配额用尽时切换到备用部署；如果想把流量分摊到所有成员，
请选择**轮询**。

![添加后端池，已预填故障转移模式下的三个成员和一个 API 路径](docs/images/onboarding-4-create-pool.png)

点击**发布到 APIM**。应用会准确列出它将创建的内容。再次点击**发布到 APIM** 确认。

![确认发布：后端池、它的三个成员和新的 API](docs/images/onboarding-5-publish.png)

### 6. 允许 APIM 调用你的模型，并获取密钥

后端池会打开到**调用测试**选项卡，它会带你完成剩下的步骤：

1. **全部授予**为 APIM 的托管标识在每个 Foundry 资源上分配 *Cognitive Services OpenAI User* 角色，
   这样 APIM 无需 API 密钥即可调用你的模型。这需要一个能在这些资源上分配角色的角色
   （*所有者*、*用户访问管理员*或*基于角色的访问控制管理员*）。如果没有，顶栏的**权限检查**会给出一份
   可以直接发给 Azure 管理员的申请。
2. **为 … 创建测试密钥**会创建一个只能访问此 API 的 APIM 订阅密钥，并自动填入。

![调用测试选项卡：全部授予，以及创建测试密钥](docs/images/onboarding-6-grant-and-key.png)

### 7. 发送测试请求

点击**发送请求**。请求会经过 APIM 和后端池，你会得到模型的回答以及处理请求的区域。如果刚刚授予访问权限，
Azure 最多可能需要 10 分钟才能使角色生效；在此之前页面会解释错误原因，请稍后重试。

![在这里测试：200，调用成功，由瑞典中部区域处理](docs/images/onboarding-7-test-call.png)

你的应用像调用任何 OpenAI 终结点一样调用后端池。页面下方的**发送请求**部分提供 curl、Python、
JavaScript 和 PowerShell 代码，并已填好你的 URL。Python 示例：

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

### 8. 观察运行情况

**指标监控**选项卡显示请求数、可用性、返回给客户端的 429 和延迟，以及每个成员承担的流量份额。
**路由预览**可以模拟成员故障，让你在真正发生之前看到故障转移的过程。

![指标监控选项卡：请求数、可用性、429、延迟和每个成员的流量份额](docs/images/onboarding-8-metrics.png)

就这些。其他功能（更多后端池、权重、会话感知、绑定你自己的 API、清理）都在同一个界面中；
[功能一览](#功能一览)列出了全部功能。

> [!CAUTION]
> 应用以**你**的身份操作 Azure，作用于你**真实**的 APIM 服务。所有更改立即生效。

**如果遇到问题**

- 顶栏的**权限检查**会重新运行设置检查。凡是你自己的 Azure 权限无法修复的问题，都会变成一份发给管理员的申请：
  角色、作用域、一条 `az` 命令和一个门户链接。
- `python scripts/doctor.py` 会说明你的电脑或网络有什么问题（Python 版本、代理、公司 TLS 证书、DNS），
  以及在你的操作系统上如何修复。
- 在代理后面或受限网络中？请参阅[网络代理与证书](#网络代理与证书)。

**不用 Azure 试用。** 在克隆的仓库中，`scripts/demo_server.py` 会让同一个界面连接一个带有示例部署的内存中
APIM，上面的截图就是这样制作的。它需要后端的虚拟环境（启动脚本会创建）和构建好的界面（Node.js 22+）：

```sh
cd frontend && npm ci && npm run build && cd ..
backend/.venv/Scripts/python scripts/demo_server.py    # macOS 和 Linux 上为 bin/python；http://127.0.0.1:8010
```

## 功能一览

| 功能 | 说明 |
| --- | --- |
| **设置向导** | 检查登录、APIM 服务、你的权限以及 APIM 对 Foundry 的访问权限。能用你的 Azure 权限修复的就直接修复，其余的生成一份可以直接发给管理员的申请。在本地还可以选择、新建或删除 APIM 服务。 |
| **导览** | 面向 APIM 新手的两分钟页面导览。设置完成后自动运行一次；顶栏的**导览**可以重新播放。 |
| **部署发现** | 按模型分组，列出你的订阅中的 Azure OpenAI / Foundry 部署，并把选中的部署导入到新的或已有的后端池。 |
| **部署模型** | 用你自己的 Azure 权限在 Foundry 帐户中创建按令牌计费的 OpenAI 模型部署，无需打开 Foundry 门户。 |
| **成员** | 在所属后端池中编辑。除了 Foundry 部署，还可以添加任何兼容 OpenAI 的终结点。不再被任何后端池使用时，应用创建的成员会被删除。 |
| **断路器** | 每个后端池一条规则，写入此应用创建的每个成员后端。 |
| **后端池** | 创建和编辑包含 2–30 个成员的 `Pool` 后端，支持**故障转移**、**轮询**或**加权**模式，可选会话感知。 |
| **费用** | 显示 APIM 服务本月的花费和所选层的标价。点击顶栏中的 APIM 名称打开。 |
| **删除** | 删除一个后端池，或此应用在服务上创建的全部内容，删除前会准确列出将被删除的内容。在本地还可以软删除此应用创建的服务（48 小时内可恢复）。 |
| **路由预览** | 显示 APIM 如何通过后端池路由流量，并模拟故障、断路器跳变和恢复。 |
| **后端池 API** | 创建后端池时会同时为它创建并绑定一个 HTTP API，客户端马上就有可调用的终结点。 |
| **调用方法** | 获取订阅密钥，在页面上发送测试请求，并提供可直接复制的客户端代码。 |
| **指标监控** | 基于 Azure Monitor 的仪表板：后端池的请求数、可用性、429 和延迟，以及每个成员的流量份额和令牌数。 |
| **API 绑定** | 通过策略把已有 API 指向后端池，写入前先预览差异。解除绑定会逐字节恢复原来的策略。 |
| **同一模型** | 如果后端池中的 Foundry 成员提供不同的模型，则拒绝保存。模型从 Azure 读取，而不是根据部署名称猜测。 |
| **公开部署** | 部署在 Container Apps 上的网站，任何人都可以用自己的 Microsoft 帐户登录，并以自己的 Azure 权限工作；服务器不保存任何内容。 |
| **安全性** | 每次更新都基于 ETag 条件请求。应用只删除自己创建的内容，并且只在你确认过之后才删除；从不改写不是它写入的策略；带有受保护 ID 前缀（`POOL_MANAGER_PROTECTED_PREFIXES`）的资源保持只读。 |

默认值：
- **断路器：** 60 秒内出现 1 次 429 / 408 / 5xx / 连接失败 / 超时即跳变，跳变持续 60 秒，并遵循 `Retry-After`。
- **重试：** 2 次，间隔 1 秒，第一次重试立即进行。
- **后端超时：** 120 秒。

### 发现的部署

- **列出哪些部署：** 只列出 `OpenAI` 或 `AIServices` 帐户上状态为 `Succeeded` 的 **OpenAI 格式**部署。合作伙伴模型（Anthropic、DeepSeek、xAI 等）使用其他推理路径，会被跳过。
- **成员 URL：** 每个导入的成员使用 `https://<account>.openai.azure.com/openai/deployments/<deployment>`。因此同一模型名称不同的部署，例如 `gpt-5.6-luna` 和 DataZone 的 `gpt-5.6-luna-2`，可以放进同一个后端池。
- **客户端如何调用：** APIM 会追加操作路径，所以客户端调用 `https://<apim>/<api-path>/chat/completions?api-version=…`，并带上请求头 `Ocp-Apim-Subscription-Key`。应用为后端池创建的 API 已经是这种形式。如果改为绑定你自己的 API，它必须公开 `/chat/completions`（或通配的 `/*`），而不是 `/deployments/{id}/…`。
- **身份验证：** 成员使用 **APIM 托管标识**进行身份验证，因为大多数帐户禁用了密钥身份验证。设置向导和选择栏会检查以下前提条件，并在你有权限时修复：
  1. 必须启用 APIM 的系统分配托管标识（**启用托管标识**）。
  2. 它需要每个帐户上的 **Cognitive Services OpenAI User** 角色（**授予访问权限**，或一份带有完整 `az role assignment create` 命令的管理员申请）。
  3. 应用为后端池创建的 API 已经会发送托管标识令牌。绑定你自己的 API 时，请把托管标识资源设为 `https://cognitiveservices.azure.com`。
- **缓存：** 结果缓存 5 分钟。**刷新**会重新扫描，25 个帐户大约需要 15 秒。

## 部署到 Azure

前置条件：[azd](https://aka.ms/azd)、Azure CLI 以及 Node.js 20.19+ 或 22.12+。你需要在 APIM 资源组上
拥有创建自定义角色和角色分配的权限（所有者或用户访问管理员）。已有的 APIM 实例是可选的：如果不设置
`APIM_NAME`，部署会在其资源组中创建一个。

**1. 创建用于登录的 Entra 应用注册**（只需一次）：

```sh
az ad app create --display-name "APIM Backend Pool Manager" \
  --sign-in-audience AzureADMyOrg --enable-id-token-issuance true
# 记下 appId。首次预配后再添加重定向 URI（第 3 步）。
```

**2. 配置并预配：**

```sh
cd apim-backend-pool
azd env new pool-manager
# 使用已有的 APIM 服务……
azd env set APIM_NAME <apim-name>
azd env set APIM_RESOURCE_GROUP <apim-resource-group>
# azd env set APIM_SUBSCRIPTION_ID <id>   # 仅当 APIM 位于其他订阅时
# ……或者不设置 APIM_NAME，在 azd 资源组中创建 apim-pool-<suffix>：
# azd env set APIM_SKU BasicV2            # BasicV2（默认）、StandardV2 或 Developer（30-40 分钟，无 SLA）
# azd env set APIM_PUBLISHER_EMAIL you@contoso.com   # 默认为 POOL_OWNERS 的第一项
azd env set ENTRA_CLIENT_ID <appId>
azd env set POOL_OWNERS "you@contoso.com"  # 可以进行更改的 UPN 或对象 ID，以逗号分隔
azd provision --preview                    # 先查看 what-if 结果
azd up
```

**3. 注册输出中 `AUTH_REDIRECT_URI` 给出的重定向 URI：**

```sh
az ad app update --id <appId> --web-redirect-uris "$(azd env get-value AUTH_REDIRECT_URI)"
```

打开 `WEB_APP_URL` 并登录。`POOL_OWNERS` 中列出的用户可以进行更改。启用委托的 Azure 访问（见下文）后，
自身 Azure 角色已经允许应用所做的每项 APIM 写入操作的用户也可以进行更改。其他人都是只读的。如果要限制谁能登录，
请在应用的企业应用程序上设置*需要分配*。

### 委托的 Azure 访问（可选，推荐）

应用标识负责所有后端池操作，从不获得更改角色分配的权限。为了让设置向导能够**修复**访问问题（启用 APIM 的托管标识、
在 Foundry 上为它分配角色），并检查每个用户能做什么，Easy Auth 还会为登录用户获取一个 Azure 资源管理器令牌。
修复操作随后以该用户自己的 Azure 权限运行，并在活动日志中显示为该用户。如果不启用，向导仍会检查所有内容并生成管理员申请。

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
| API Management 服务 `apim-pool-*`（仅在未设置 `APIM_NAME` 时） | 默认为 `BasicV2`（`APIM_SKU`），带有供成员使用的系统分配托管标识，并标记 `managed-by: apim-backend-pool`。从创建起开始计费 |
| Linux App Service 计划（B1）+ Web 应用（Python 3.12） | 仅 HTTPS、TLS 1.2，禁用 FTP/SCM 基本身份验证，系统分配托管标识 |
| 用户分配托管标识 `id-pool-auth-*`（仅在启用委托的 Azure 访问时） | 作为 Easy Auth 的联合凭据，代替客户端密码。它没有任何 Azure 角色 |
| App Service 身份验证（Easy Auth） | 除 `/health` 外的每个路径都需要 Entra 登录 |
| APIM 服务上的自定义角色 *APIM Backend Pool Manager* | 用于指标监控选项卡的 `Insights/metrics/read`、`backends/*`、`namedValues/*`、`apis/read,write,delete`（只在删除后端池时删除它自己创建的 API）、`apis/operations/write` 和 `apis/policies/read,write`，以及让设置检查应用自身访问权限的 `Authorization/permissions/read` |
| 每个发现订阅上的自定义角色 *APIM Backend Pool Deployment Reader* | 用于指标监控选项卡的 `Insights/metrics/read`、`CognitiveServices/accounts/read` 和 `accounts/deployments/read`，以及只读的 `Authorization/permissions`、`roleAssignments` 和 `roleDefinitions`，让设置可以检查 APIM 在每个帐户上的角色。它没有 `listKeys`，也不能更改角色。用 `enableDeploymentDiscovery=false` 关闭，用 `discoverySubscriptionIds` 选择订阅（默认为 APIM 所在订阅） |

APIM 自身的标识在两种情况下需要额外的 RBAC：
- **Key Vault 机密：** 如果成员的机密引用 Key Vault，APIM 托管标识需要该保管库上的 *Key Vault Secrets User* 角色。
- **托管标识访问 Foundry：** 如果成员使用托管标识进行身份验证，APIM 标识需要每个成员帐户上的 *Cognitive Services OpenAI User*（或任何允许推理的角色）。设置向导会检查这一点，并可以授予。

### 配置

| 设置 | 默认值 | 用途 |
| --- | --- | --- |
| `POOL_MANAGER_SUBSCRIPTION_ID` / `_RESOURCE_GROUP` / `_APIM_NAME` | 部署时必填 | 目标 APIM 服务。在本地可以不设置，在设置页上选择或新建服务；三项都设置则固定为该服务 |
| `POOL_MANAGER_TENANT_ID` | 来自设置页 | APIM 订阅所在的租户，本地令牌会向该租户请求 |
| `POOL_MANAGER_OWNERS` | 空 | 谁可以进行更改 |
| `POOL_MANAGER_RBAC_WRITERS` | `true` | 自身 Azure 角色允许应用所做的每项 APIM 写入操作的用户也可以进行更改。需要委托的 Azure 访问 |
| `POOL_MANAGER_DELEGATED_ACCESS` | `false` | 由 `enableDelegatedAzureAccess` 设置：Easy Auth 提供用户的 ARM 令牌，让设置可以修复访问问题 |
| `POOL_MANAGER_POOL_API_VERSION` | `2025-09-01-preview` | 读写后端池使用的 API 版本。使用 `2024-05-01` 时会禁用会话感知，因为 GA 版本无法携带 `sessionAffinity` |
| `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS` | APIM 所在订阅 | 扫描部署的订阅，以逗号分隔 |
| `POOL_MANAGER_PROTECTED_PREFIXES` | 空 | 以只读方式显示的 ID 前缀，以逗号分隔，用于由其他工具管理的资源（例如 `finops-`） |
| `POOL_MANAGER_ID_PREFIX` | `pool-` | 新资源的建议前缀 |
| `POOL_MANAGER_AUTH_MODE` | `easy_auth` | `disabled` 仅用于本地开发；在 App Service 上应用会拒绝以这种方式启动 |
| `POOL_MANAGER_SYSTEM_TRUST` | `true` | 在本地使用操作系统证书存储验证 TLS（适用于 TLS 检查背后的公司 CA） |
| `AZURE_CLIENT_ID` | — | 使用用户分配托管标识，而不是系统分配托管标识 |

## 公开部署到 Azure Container Apps

一个这样的部署运行在 https://ca-pool-public-5xgy5gsq3leyq.ambitiousground-f590833b.swedencentral.azurecontainerapps.io/。

`deploy/container-apps/` 把应用部署成一个公开网站：任何工作或学校帐户都可以用自己的 Microsoft Entra
身份登录，应用管理的是**他们自己的 Azure 角色**允许的 APIM 服务和 Foundry 部署。不需要预先为任何客户做配置，
应用自身也没有任何 Azure 访问权限。

| | [部署到 Azure](#部署到-azure)（App Service） | 公开部署（Container Apps） |
| --- | --- | --- |
| 谁可以登录 | 你自己租户的用户 | 任何工作或学校租户（或允许列表中的租户） |
| APIM 服务 | 一个，部署时固定 | 每个用户选择或新建自己的服务 |
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
设置修复以及切换服务。所有访客共用一个缓存会话，并有频率限制（每个客户端地址每分钟 300 个请求，所有访客合计 3000 个）。
该服务和订阅中的内容（例如后端池成员的 URL 和 API 策略）对任何拿到网址的人可见，所以请使用演示用的服务。

**限制。** 仅支持 Azure 公有云。一次登录对应一个租户；选择另一个租户中的服务时会登录到该租户。部署发现扫描所选
服务所在的订阅。网关使用自定义域的服务可以管理，但不能在应用中发送测试调用。

| 设置 | 默认值 | 用途 |
| --- | --- | --- |
| `POOL_MANAGER_AUTH_MODE` | `entra`（由模板设置） | 公开部署的登录方式 |
| `POOL_MANAGER_ENTRA_CLIENT_ID` | 来自钩子 | 多租户应用注册 |
| `POOL_MANAGER_FEDERATED_IDENTITY_CLIENT_ID` | 来自模板 | 用作应用联合凭据的托管标识 |
| `POOL_MANAGER_ENTRA_ALLOWED_TENANTS` | 空（`ALLOWED_TENANTS`） | 允许其用户登录的租户 ID；空表示任何工作或学校租户 |
| `POOL_MANAGER_SESSION_IDLE_SECONDS` | `1800` | 用户最后一次请求后经过这么久，会话（及其 Azure 令牌）结束 |
| `POOL_MANAGER_MAX_SESSIONS` | `500` | 每个副本保留的会话数；最久未使用的先移除 |
| `POOL_MANAGER_USER_REQUESTS_PER_MINUTE` | `600` | 每个用户每分钟的请求数 |

## 本地运行

在本地，应用运行在你的电脑上，并以**你**的身份操作 Azure。它只需要 **Python 3.11+**。只有开发浏览器界面时才需要
Node.js，Azure CLI 是可选的。

**最快的方式：发布包。** 从 [Releases](https://github.com/guming3d/apim-backend-pool-manager/releases) 下载
`apim-backend-pool-<version>.zip`。它内置了浏览器界面，因此不需要 Node.js 和 git。解压后运行：

```powershell
scripts\start-local.cmd                 # Windows；不受 PowerShell 执行策略影响
```

```sh
scripts/start-local.sh                  # macOS、Linux、WSL
```

打开 http://127.0.0.1:8000。设置页会先让你登录，然后让你选择或新建 APIM 服务（选择保存在
`backend/.pool-manager.json` 中）。如果要固定一个服务，请在 `backend/.env` 中设置
`POOL_MANAGER_SUBSCRIPTION_ID`、`_RESOURCE_GROUP` 和 `_APIM_NAME`。

**在克隆的仓库中**，安装了 Node.js 22+ 时，同样的脚本会在 `:5174` 上运行 Vite 开发服务器。没有 Node.js 时，
如果界面已经构建过，它们会从 `backend/pool_manager/static` 提供界面（`-Prebuilt` / `--prebuilt` 强制如此）。

```powershell
.\scripts\start-local.ps1               # -Tui -Prebuilt -BackendPort 8001 -FrontendPort 5175 -SkipInstall -SkipChecks
```

```sh
scripts/start-local.sh                  # --tui --prebuilt --backend-port 8001 --frontend-port 5175 --skip-install --skip-checks
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
  https://microsoft.com/devicelogin 并输入显示的验证码。在 TUI 中，在 Setup 视图按 `d`。
- 在应用中完成的登录会在重启后**保留**，保存在操作系统的加密存储中（Windows 上为 DPAPI，macOS 上为钥匙串，
  Linux 上为 libsecret）。`backend/.pool-manager-auth.json` 只保存帐户记录。在无法加密的环境中（没有密钥环的 Linux 或
  WSL），应用会把登录状态保存在内存中并给出提示；它从不以明文写入令牌。**退出此登录**会清除它。

### 网络代理与证书

- **代理：** 设置 `HTTPS_PROXY=http://proxy:8080`（以及 `NO_PROXY=localhost,127.0.0.1`）。应用、pip 和 npm 都会读取它。
- **TLS 检查**（公司证书）：把公司 CA 安装到操作系统证书存储中。在本地，应用会使用该存储验证 TLS
  （[truststore](https://pypi.org/project/truststore/)；`POOL_MANAGER_SYSTEM_TRUST=false` 可关闭）。pip 24.2+ 也是如此。
  对于较旧的 pip，设置 `PIP_CERT=<ca.pem>`；对于 npm，设置 `NODE_EXTRA_CA_CERTS=<ca.pem>`。
- **软件包镜像：** `PIP_INDEX_URL=https://<mirror>/simple/` 和 `npm_config_registry=https://<mirror>/`。doctor 会检查镜像，
  而不是 PyPI 和 npm。
- 应用需要访问 `https://login.microsoftonline.com` 和 `https://management.azure.com`，测试调用还需要访问你的 APIM 网关。
  doctor 能区分 DNS 失败、超时、不受信任的证书以及要求凭据的代理。

### 远程机器和 WSL

通过 SSH 时，用验证码登录，并转发端口以在你自己的电脑上打开界面：`ssh -L 8000:localhost:8000 <host>`
（使用开发服务器时为 `5174`）。也可以使用完全不需要浏览器的终端界面（`--tui`）。在 WSL 中，请在 WSL 内用 Linux 版
Python 运行 `scripts/start-local.sh`；`.gitattributes` 会让 shell 脚本即使在 Windows 检出中也保持 LF 换行符。

### 手动运行

```sh
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/macOS 上为 bin/pip
cp .env.example .env                       # 无需修改；在设置页上选择 APIM
.venv/Scripts/python -m uvicorn pool_manager.app:app --reload           # 如果已构建，会提供 static/

cd ../frontend && npm ci && npm run dev    # http://localhost:5174，把 /api 代理到 :8000
```

在克隆的仓库中运行 `npm run build` 后，`python scripts/package-release.py` 会构建发布 zip。推送 `v*` 标签会发布一个
（`.github/workflows/release.yml`）。

### 终端界面

同一个应用也可以在终端中运行，适合习惯在终端里工作的人。`tui/` 是一个独立的 [Textual](https://textual.textualize.io/)
应用，采用 Omarchy 风格的 TUI：Tokyo Night 配色、圆角窗格（获得焦点的窗格使用强调色）、顶栏中作为工作区的视图，以及
按键提示页脚。它只是后端 `/api` 的客户端，因此所有规则（ETag、受保护前缀、同一模型检查、谁可以写入）仍然集中在一处。

```powershell
.\scripts\start-local.ps1 -Tui           # 后端 + TUI；不需要 Node.js
```

```sh
scripts/start-local.sh --tui
# 或手动运行：cd tui && python -m venv .venv && .venv/bin/pip install -r requirements.txt
#             .venv/bin/python -m pool_tui              # 复用 :8000，或自行启动 backend/
#             .venv/bin/python -m pool_tui --api http://127.0.0.1:8000
```

| 按键 | |
| --- | --- |
| `1` `2` `3` | 后端池、模型部署、设置 |
| `n` `e` `D` | 新建、编辑（模式、成员、主部署、权重、会话感知）、删除后端池 |
| `a` `b` `u` | 为后端池创建 API、绑定已有 API、解除绑定 |
| `t` | 测试 API：选择或创建订阅密钥，发送请求，查看响应的含义 |
| `m` | 后端池指标和成员表的监控时间范围（1h、6h、24h、7d） |
| `space` `i` `c` `R` | 模型部署：选择、作为成员导入、导入并创建后端池、重新扫描 Azure |
| `enter` `c` | 设置：运行光标所在项的修复、选择 APIM 服务 |
| `d` `o` | 设置：用验证码登录（例如通过 SSH 时没有浏览器）、退出应用中的登录 |
| `d` / `enter` | 在选择 APIM 时：删除此应用创建的服务（输入其名称）、从“最近删除”中恢复服务 |
| `?` `q` | 帮助、退出 |

它涵盖了日常操作。新建 APIM 服务、路由模拟器和策略差异在 Web 界面中。TUI 的文字只有英文。

> [!CAUTION]
> 本地开发使用**你**的 Azure 身份，作用于**真实**的 APIM 服务。所有更改立即生效。

## 测试

```sh
cd backend && .venv/Scripts/pip install pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
cd ../frontend && npm test && npm run build
cd ../tui && .venv/Scripts/pip install -r requirements.txt -r ../backend/requirements.txt pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
```

后端测试使用一个内存中的 APIM（`tests/fake_apim.py`），它会强制执行 ETag 以及 `If-Match` / `If-None-Match`。
不需要 Azure 访问。TUI 测试通过 `httpx.ASGITransport` 驱动连接到同一个模拟服务的真实后端应用。
