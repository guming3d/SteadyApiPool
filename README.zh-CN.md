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

<table>
  <tr>
    <th width="50%">Azure API 管理</th>
    <th width="50%">LiteLLM（容器应用或 AKS）</th>
  </tr>
  <tr>
    <td><a href="images/apim-architecture.gif"><img src="images/apim-architecture.gif" alt="APIM：管理应用发布一个包含三个部署的后端池；主部署返回 429 时断路器跳变 60 秒，重试发往下一个成员" width="100%"></a></td>
    <td><a href="images/litellm-architecture.gif"><img src="images/litellm-architecture.gif" alt="LiteLLM：管理应用发布一个包含三个部署的后端池（模型组）；主部署返回 429 时冷却 60 秒，LiteLLM 在下一个成员上重试" width="100%"></a></td>
  </tr>
</table>

*同一个后端池在两种网关上：主部署返回 429，网关在下一个成员上重试，并让主部署暂停 60 秒，之后再把流量交还给它。
动画中的文字为英文。*

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

### 能承载多少流量？

在 Azure 上测得（2026 年 10 月），每种网关都按应用的设置创建，由同一区域的一台虚拟机施加负载；表中是它承载的
每分钟请求数，以及在每分钟 600 个请求时增加的中位数延迟：

| | APIM 基本 v2 | APIM 标准 v2 | 容器应用上的 LiteLLM | AKS 上的 LiteLLM |
| --- | --- | --- | --- | --- |
| 流式聊天（输入 2,000 个 token，输出 500 个） | 4,500 | 12,000 | 3,720 | 11,280 |
| 简短回答，非流式 | ≥ 22,500 | ≥ 22,500 | ≥ 22,500 | ≥ 15,000 |
| 代理循环的一步（输入 50,000 个 token） | 10,500 | 16,860 | ≥ 22,500 | ≥ 15,000 |
| 增加的延迟（中位数） | 不到 10 毫秒 | 不到 10 毫秒 | 20–70 毫秒 | 20–60 毫秒 |

某个部署返回 429 或 500 时，两种网关都在一秒内把它的流量切换到下一个部署；使用真实模型时，超出一个部署配额
的调用全部由后端池响应，而只用那一个部署时最多有 65% 被拒绝。规模、价格、故障转移的细节和测试方法见
[性能测试](docs/benchmarks.zh-CN.md)。

## 快速开始

应用在你自己的电脑上运行，只需要 **Python**：不需要 Azure CLI、Node.js、Docker 或配置文件，也不需要任何 APIM
或 LiteLLM 经验。应用会检查缺少什么，并和你一起补上。整个过程需要十分钟到半小时，如果还没有网关，大部分时间是
等待 Azure 创建它。每一步的所有选项都在[分步指南](docs/getting-started.zh-CN.md)中。

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

接下来按[快速开始](docs/quick-start.zh-CN.md)从第 2 步继续：登录、选择或部署网关、把部署放进后端池、发布后端池
并发送第一个调用，每一步都有截图。

> [!CAUTION]
> 应用以**你**的身份操作 Azure，作用于你**真实**的网关（对于 LiteLLM，还会使用它的主密钥）。所有更改对所有客户端
> 立即生效。

## 文档

- [快速开始](docs/quick-start.zh-CN.md)：第 2–8 步，每一步都有截图：登录、选择或部署网关、创建并发布后端池、
  获取密钥、发送第一个调用并查看指标。
- [分步指南](docs/getting-started.zh-CN.md)：每一步的所有选项、在容器应用或 AKS 上部署 LiteLLM 网关、客户端代码、
  性能测试、问题排查，以及不用 Azure 的演示。
- [功能与工作原理](docs/features.zh-CN.md)：应用的全部功能、后端池在 APIM 和 LiteLLM 中的对应关系，以及 LiteLLM
  网关部署会创建什么、如何维护。
- [性能测试](docs/benchmarks.zh-CN.md)：每种网关能承载多少流量、增加多少延迟，以及部署故障时客户端看到什么，均在 Azure
  上测得。
- [托管到 Azure](docs/deploy.zh-CN.md)：用 `azd` 为团队部署到 App Service，以及公开的多租户应用。
- [本地运行与开发](docs/development.zh-CN.md)：各操作系统的前置条件、登录、代理与证书、WSL、项目结构和测试。
