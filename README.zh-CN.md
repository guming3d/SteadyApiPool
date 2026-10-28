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

## 选择哪种网关？

| | Azure API 管理 | LiteLLM |
| --- | --- | --- |
| 何时选择 | 你的组织已经在运行 APIM，或想要一个带有策略、产品和门户的 Azure 托管服务 | 你想要一个带有模型名、别名和按密钥限制的 OpenAI 风格终结点，或一个可以在任何地方运行的开源网关 |
| 应用会设置什么 | 一个 APIM 服务（你已有的，或新建的基本 v2 服务），每个后端池一个 `Pool` 后端，以及一个绑定到它的 API | 容器应用上带 PostgreSQL 的 LiteLLM 代理（一步完成）或新的 AKS 群集，或连接你已有的网关；每个后端池一个模型组 |
| 客户端如何调用 | `https://<apim>.azure-api.net/<api-path>/chat/completions?api-version=…`，带上 `Ocp-Apim-Subscription-Key` | `https://<gateway>/v1/chat/completions`，使用虚拟密钥，并把后端池 ID（或别名）作为模型 |
| 失败处理 | 每个成员一个断路器，API 策略中的重试，可选的会话感知 | 每个成员的冷却，在另一个成员上重试，Responses API 的后续请求固定发往最初响应的部署 |
| 费用 | 服务所选的层（建议从基本 v2 开始；开发人员层没有 SLA），按小时计费 | 一个容器应用副本起价约每月 $46；部署前，规模计算器会按你的流量和区域为规模计算价格（标价） |
| 其他形式 | [公开应用](docs/deploy.zh-CN.md#公开部署到-azure-容器应用)：任何人都可以用自己的 Azure 帐户登录 | — |

两种网关以相同的标识规则管理同样的 Foundry 部署（网关用它的托管标识调用部署，不使用密钥），你也可以在设置页上
在两者之间切换。

## 快速开始

应用在你自己的电脑上运行，只需要 **Python**：不需要 Azure CLI、Node.js、Docker 或配置文件，也不需要任何 APIM
或 LiteLLM 经验。应用会检查缺少什么，并和你一起补上。整个过程需要十分钟到半小时，如果还没有网关，大部分时间是
等待 Azure 创建它。每一步的所有选项都在[分步指南](docs/getting-started.zh-CN.md)中。

> [!NOTE]
> 截图为英文界面。浏览器语言为中文时界面显示中文，也可以用顶栏的语言按钮切换。下文使用中文界面上的按钮名称。

**你需要准备**

- **Python 3.11 或更高版本**：Windows 上运行 `winget install -e --id Python.Python.3.13`，macOS 上运行
  `brew install python@3.13`，其他系统请参阅[各操作系统的前置条件](docs/development.zh-CN.md#各操作系统的前置条件)。
- **一个 Azure 帐户**：能管理已有的网关或创建网关（*参与者*），并能读取你的 Azure OpenAI / Foundry 资源（*读者*）。
- **一些 Azure OpenAI / Foundry 模型部署**，最好是同一个模型部署在多个区域。应用也可以帮你创建。

### 1. 下载并启动

从 [Releases](https://github.com/guming3d/SteadyApiPool/releases) 下载 `SteadyApiPool-<version>.zip`，解压后运行：

```powershell
scripts\start-local.cmd        # Windows
```

```sh
scripts/start-local.sh         # macOS、Linux、WSL
```

首次运行会安装应用所需的依赖（大约一分钟）。然后打开 **http://127.0.0.1:8000**。

### 2. 登录 Azure

点击**用浏览器登录**；在没有浏览器的机器上点击**改用验证码登录**。应用会记住登录状态。

![设置页的登录、AI 网关、权限和网关 → Foundry 四步中的第 1 步：用浏览器登录，或改用验证码登录](docs/images/onboarding-1-sign-in.png)

### 3. 选择网关

选择 **LiteLLM 网关**或 **Azure API 管理**（之后也可以切换），然后在已有的网关上点击**使用此网关**或**使用此服务**。

![设置页第 2 步：选择 LiteLLM 网关或 Azure API 管理，以及订阅中的 LiteLLM 网关，每个都有“使用此网关”按钮，另有“部署网关”和“连接已有网关”](docs/images/onboarding-2-choose-gateway.png)

还没有 APIM 服务？**新建 APIM 服务**会填好所有内容（基本 v2 层），并等到服务就绪，通常需要几分钟。

![设置页第 2 步，选择了 Azure API 管理：订阅中的 APIM 服务，每个都有“使用此服务”按钮，另有“新建 APIM 服务”](docs/images/onboarding-2-choose-apim.png)

![新建 APIM 服务表单，已预填基本 v2 层](docs/images/onboarding-2b-create-apim.png)

还没有 LiteLLM 网关？**部署网关**会按你的峰值流量确定网关规模，推荐 Azure 容器应用或 AKS 并说明计算过程，然后
完成部署（5–25 分钟）。规模背后的数字见[确定网关规模](docs/getting-started.zh-CN.md#确定网关规模)；如果你已在运行
LiteLLM 代理，点击**连接已有网关**。

![按你的流量确定网关规模：峰值 RPM、token、超时和流式传输，推荐容器应用的规模并展开了计算过程，旁边是 AKS 的规模](docs/images/onboarding-2b-deploy-gateway.png)

### 4. 选择要放进后端池的部署

打开**模型部署**选项卡，点击**用此模型创建后端池**。还没有任何部署？**部署模型**会帮你创建。

![模型部署选项卡：gpt-5.6-luna 部署在三个区域，带有“用此模型创建后端池”按钮](docs/images/onboarding-3-deployments.png)

### 5. 发布后端池

后端池打开时所有内容都已填好。**故障转移**把所有流量发给主部署，在它失败时切换到备用部署；**轮询**和**加权**
则把流量分摊到各个成员。

![添加后端池，已预填故障转移模式下的三个成员和冷却设置](docs/images/onboarding-4-create-pool.png)

点击**发布到 APIM**（或**发布到网关**），查看应用将写入的全部内容，然后确认。

![确认发布：新的后端池及其三个成员](docs/images/onboarding-5-publish.png)

### 6. 允许网关调用你的模型，并获取密钥

在**调用测试**选项卡上，**全部授予**让网关的托管标识无需 API 密钥即可调用你的 Foundry 资源，**创建测试密钥**
会获取一个只能调用此后端池的密钥。

![调用测试选项卡：终结点与模型名、调用前检查，以及获取虚拟密钥和“创建测试密钥”](docs/images/onboarding-6-grant-and-key.png)

### 7. 发送测试请求

点击**发送请求**，得到模型的回答以及处理请求的成员。页面下方的**发送请求**部分提供 curl、Python、JavaScript 和
PowerShell 的客户端代码。

![在这里测试：200，调用成功，由后端池的主成员 pool-aoai-westus3-gpt-5-6-luna-dz 响应](docs/images/onboarding-7-test-call.png)

### 8. 观察运行情况

**指标监控**选项卡显示请求数、可用性、429 和延迟，以及每个成员承担的流量份额。**路由预览**可以模拟成员故障。

![指标监控选项卡：请求数、可用性、429、延迟和每个成员的流量份额](docs/images/onboarding-8-metrics.png)

> [!CAUTION]
> 应用以**你**的身份操作 Azure，作用于你**真实**的网关（对于 LiteLLM，还会使用它的主密钥）。所有更改对所有客户端
> 立即生效。

遇到问题？顶栏的**权限检查**会重新运行设置检查，并为你的权限无法修复的问题写好一份发给 Azure 管理员的申请；
`python scripts/doctor.py` 会说明电脑和网络的问题。更多内容见[如果遇到问题](docs/getting-started.zh-CN.md#如果遇到问题)。

## 文档

- [分步指南](docs/getting-started.zh-CN.md)：每一步的所有选项、在容器应用或 AKS 上部署 LiteLLM 网关、客户端代码、
  性能测试、问题排查，以及不用 Azure 的演示。
- [功能与工作原理](docs/features.zh-CN.md)：应用的全部功能、后端池在 APIM 和 LiteLLM 中的对应关系，以及 LiteLLM
  网关部署会创建什么、如何维护。
- [托管到 Azure](docs/deploy.zh-CN.md)：用 `azd` 为团队部署到 App Service，以及公开的多租户应用。
- [本地运行与开发](docs/development.zh-CN.md)：各操作系统的前置条件、登录、代理与证书、WSL、项目结构和测试。
