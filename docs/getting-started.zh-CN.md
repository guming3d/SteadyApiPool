[← SteadyApiPool](../README.zh-CN.md)

[English](getting-started.md) | **简体中文**

# 分步指南

本指南在你自己的电脑上运行应用。你只需安装 Python，不需要 Azure CLI、Node.js、Docker、配置文件，也不需要
任何 APIM 或 LiteLLM 经验；应用会检查缺少什么，并和你一起补上。（如果想给团队一个共享的网址，请参阅
[部署到 Azure](deploy.zh-CN.md#部署到-azure)。）

> [!NOTE]
> 截图为英文界面。浏览器语言为中文时界面显示中文，也可以用顶栏的语言按钮切换。下文使用中文界面上的按钮名称。

**你需要准备**

- **Python 3.11 或更高版本。** Windows 上运行 `winget install -e --id Python.Python.3.13`，
  macOS 上运行 `brew install python@3.13`，其他系统请参阅[各操作系统的前置条件](development.zh-CN.md#各操作系统的前置条件)。
- **一个 Azure 帐户**：能管理一个已有的网关（APIM 服务或 LiteLLM 网关的容器应用），或能在某个订阅中创建网关
  （*参与者*），并能读取你的 Azure OpenAI / Foundry 资源（*读者*）。
- **一些 Azure OpenAI / Foundry 模型部署**，最好是同一个模型部署在多个区域。如果还没有，
  应用可以帮你创建（第 4 步）。

## 1. 下载并启动

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

## 2. 登录 Azure

点击**用浏览器登录**，用你的 Azure 帐户登录。在没有浏览器的机器上（SSH、WSL、服务器），点击
**改用验证码登录**。应用会把登录状态保存在操作系统的加密存储中，所以只需登录一次。

![设置页的登录、AI 网关、权限和网关 → Foundry 四步中的第 1 步：用浏览器登录，或改用验证码登录](images/onboarding-1-sign-in.png)

## 3. 选择网关类型，然后选择网关

选择 **LiteLLM 网关**或 **Azure API 管理**（见[选择哪种网关？](../README.zh-CN.md#选择哪种网关)），之后也可以切换。应用随后会列出
你能访问的每个订阅中该类型的网关，并说明你是否有权管理它们。

![设置页第 2 步：选择 LiteLLM 网关或 Azure API 管理，以及订阅中的 LiteLLM 网关，每个都有“使用此网关”按钮，另有“部署网关”和“连接已有网关”](images/onboarding-2-choose-gateway.png)

### Azure API 管理

在你想要的 APIM 服务上点击**使用此服务**。

![设置页第 2 步，选择了 Azure API 管理：订阅中的 APIM 服务，每个都有“使用此服务”按钮，另有“新建 APIM 服务”](images/onboarding-2-choose-apim.png)

还没有 APIM 服务？点击**新建 APIM 服务**。所有内容都已填好：唯一的名称、新的资源组、区域，以及推荐的
**基本 v2** 层。确认发布者电子邮件后点击**创建并使用**。应用会等到服务就绪，通常需要几分钟
（开发人员层需要 30–40 分钟）。服务从创建起就开始计费。

![新建 APIM 服务表单，已预填基本 v2 层](images/onboarding-2b-create-apim.png)

### LiteLLM 网关

在 Azure 容器应用上的某个 LiteLLM 网关上点击**使用此网关**。

还没有网关？点击**部署网关**。应用会先按你的流量确定网关规模（见下文[确定网关规模](#确定网关规模)），
并推荐容器应用或 AKS：

- **部署到容器应用**会打开一个所有内容都已填好的表单：唯一的名称、资源组和区域，以及要部署的规模和它在该区域
  的标价。选择区域时，应用会检查该区域能否容纳网关的每个资源（见下文
  [部署之前，以及部署仍然失败时](features.zh-CN.md#部署之前以及部署仍然失败时)）。点击**部署**。应用以你自己的 Azure 权限部署 LiteLLM、PostgreSQL、一个托管标识和
  Application Insights，并跟踪部署进度，通常需要 5–15 分钟（PostgreSQL 最慢）。期间可以关闭应用，设置会接着跟踪。
  资源从创建起就开始计费。
- **在 AKS 上部署**为**新的 AKS 群集**打开同样的表单。点击**部署**后，应用分三个阶段进行，并实时显示进度（通常共
  15–25 分钟）：
  1. **Azure 资源**：群集（Microsoft Entra ID 与 Azure RBAC、工作负载标识、托管 Gateway API）、PostgreSQL、网关
     的托管标识、Application Insights，以及多于一个 Pod 时的 Redis。你会获得该群集的 *Azure Kubernetes 服务 RBAC 群集管理员*
     角色，因此部署需要资源组上的所有者或用户访问管理员权限。
  2. **在群集中安装**：通过 AKS 运行命令（你的电脑上无需 kubectl 或 Helm）安装 cert-manager、OpenTelemetry
     Collector、LiteLLM 的 Helm 图表和 HTTPS 网关。
  3. **HTTPS 证书**：Let's Encrypt 为 `https://<名称>.<区域>.cloudapp.azure.com` 签发证书；LiteLLM 在该地址上
     应答后，应用就像连接任何网关一样连接它。

  运行期间请保持此页打开（进度会推动安装继续；关闭后回到 **AI 网关**这一步即可继续）。安装失败时会显示其输出，
  并提供**重新运行安装**。此后群集**由你维护**，见 [AKS 上的 LiteLLM 网关](features.zh-CN.md#aks-上的-litellm-网关)。
- **使用已有群集（生成文件）**适用于你已有的群集：它会询问群集、其资源组和命名空间，然后生成 `values.yaml`、
  `deploy.sh` 和 `README.md`（Helm 图表、工作负载标识、PostgreSQL 灵活服务器，一个副本；README 说明更多副本需要什么）。群集需要启用 OIDC 颁发者和工作负载
  标识。运行该脚本（例如在 Azure Cloud Shell 中），然后点击**已部署，去连接**。脚本通过公共 IP 以纯 HTTP 公开
  LiteLLM，而**连接**只接受 HTTPS URL（纯 HTTP 仅限 localhost，主密钥不会以明文经过网络）。想在运行本应用的电脑上
  先试用，可保持 `kubectl -n <namespace> port-forward svc/litellm 4000:4000` 运行并连接 `http://localhost:4000`；
  正式使用时，请在它前面加上 HTTPS，并连接其 HTTPS URL。

**连接已有网关**适用于任何带数据库（`STORE_MODEL_IN_DB`）的 LiteLLM 代理：输入它的 HTTPS URL、主密钥，以及可选的
它调用 Foundry 时使用的托管标识对象 ID 和它的 Application Insights 资源 ID。应用会用网关本身验证 URL 和主密钥，
并把主密钥加密保存在本机，从不以明文写入文件。

![按你的流量确定网关规模：峰值 RPM、token、超时和流式传输，推荐容器应用的规模并展开了计算过程，旁边是 AKS 的规模](images/onboarding-2b-deploy-gateway.png)

接下来会打开一个两分钟的导览。可以跟着看，也可以跳过；顶栏的**导览**可以重新播放。

#### 确定网关规模

规模计算器会询问峰值时的流量：每分钟请求数、平均输入和输出 token 数、单次调用最长时间，以及响应是否以流式传输
返回。**更多选项**中有模型速度（快、标准或推理模型三种预设，或填写你自己的平均响应时间）、峰值之上的余量（默认
25%）、是否要承受一个可用性区域故障（默认开启），以及你们是否已在运维 AKS、是否要在网关旁运行自托管模型、是否
需要专用网络。

后端据此算出网关的规模，**计算过程**会逐步列出每一步的数字：

| 步骤 | 算法 |
| --- | --- |
| 请求和 token | 每秒请求数 = RPM ÷ 60。每分钟 token 数 = RPM ×（输入 + 输出）：即各个池中 Foundry 部署合计需要的配额 |
| 同时进行的调用 | 每秒请求数 × 每次调用的时间（首个 token 之前的时间，加上按模型速度输出的时间）；以及每个调用都拖到超时时的数量 |
| 一个副本 | 每次调用占用的网关 CPU（固定开销、请求和响应正文、每个流式 token），对比一个 LiteLLM 工作进程使用一个核心的 70%；以及它的内存能容纳多少个调用 |
| 副本 | 峰值所需的副本，加上余量，再加一个应对区域故障的副本（至少两个）。每个副本同时进行的调用超过峰值时它容纳的数量，或 CPU 超过其工作进程一个核心的 60% 时，容器应用会添加副本；AKS 按 60% CPU |
| 数据库和 Redis | 连接数足以容纳 副本数 × 10 × 2（更新时翻倍）的最小 PostgreSQL：常规用途，只有一个副本时才用可突发。多于一个副本就加 Redis，每秒 1,000 个请求或 10 个副本起通过 Redis 写入花费 |
| AKS | 容纳这些 Pod 的节点大小和范围（D4ds_v5 每个 2 个、D8ds_v5 每个 5 个、D16ds_v5 每个 12 个），至少三个跨可用性区域的节点，以及足够的出站 IP，让每个节点在所有调用都拖到超时时仍有足够的 SNAT 端口 |

除非流量排除了它，否则推荐**容器应用**。它的入口会结束 240 秒没有任何输出的请求（实测：一个不输出的调用在 241 秒
收到 504，而每 10 秒流式输出一次的调用完整运行了 300 秒），所以不流式传输且可能超过这个时间的调用，或首个 token
晚于这个时间的调用，会推荐 **AKS**；需要超过 30 个副本的峰值、已在运维 AKS 的团队和自托管 GPU 模型也是如此。每张
卡片显示该平台的规模、阻止它在那里运行的原因，以及部署按钮。随后的部署表单会按你选择的区域为这个规模计算标价，
区域检查也使用它的规格。

网关每次调用的 CPU 用量是用 `backend/tests/litellm/litellm_stack/calibrate.py` 在按本应用部署方式配置的 LiteLLM 上
实测的（Docker，模拟的上游）：每次调用约 27 毫秒，每个流式 token 约 0.75 毫秒，启用 OpenTelemetry 时每 1,000 个提示
token 再让每个流式 token 多约 0.11 毫秒；一个工作进程满载时每秒处理 26–46 个调用。**流式传输决定网关的规模**：500 个
token 的流式响应约为完整返回的 15 倍。Azure 的 CPU 可能略有不同，内存是估算值，所以部署后请按峰值对某个池做压力测试
（池的**压力测试**，或 `scripts/benchmark.py`）来确认。价格是从空闲到繁忙的零售标价，
不含免费额度、日志引入、折扣和模型用量；网关创建后，它的面板会显示本月实际花费。

不能对互联网开放的网关在两者上都可以实现，但这里没有自动化专用网络：在容器应用上，之后把环境放进虚拟网络
（或修改模板）；在 AKS 上，使用内部负载均衡器或专用入口。

## 4. 选择要放进后端池的部署

打开**模型部署**选项卡。它按模型分组，列出网关所在订阅中的每个 Azure OpenAI / Foundry 部署。（其他订阅中的部署：
在 `backend/.env` 中设置 `POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS`。）在你想要的模型上点击**用此模型创建后端池**。
如果只想放入某个模型的部分部署，勾选它们后选择**导入并创建后端池**。

![模型部署选项卡：gpt-5.6-luna 部署在三个区域，带有“用此模型创建后端池”按钮](images/onboarding-3-deployments.png)

还没有任何部署？**部署模型**会在你的某个 Foundry 资源中创建部署，然后在这里列出。

## 5. 发布后端池

后端池打开时所有内容都已填好，默认值即可使用：

- **APIM：** ID、客户端将调用的 API 路径、API节点和断路器。
- **LiteLLM：** ID（这里是 `pool-gpt-5-6-luna`，它也是客户端发送的模型名）、API节点，以及冷却、重试和超时设置。

**故障转移**把所有流量发给主部署，在它失败或配额用尽时切换到备用部署；如果想把流量分摊到所有API节点，请选择
**轮询**，或选择**加权**自行设置比例。

![添加后端池，已预填故障转移模式下的三个API节点和冷却设置](images/onboarding-4-create-pool.png)

点击**发布到 APIM**（或**发布到网关**）。应用会准确列出它将写入的内容。再次点击确认。更改会立即在网关上生效。

![确认发布：新的后端池及其三个API节点](images/onboarding-5-publish.png)

## 6. 允许网关调用你的模型，并获取密钥

后端池会打开到**调用测试**选项卡，它会带你完成剩下的步骤：

1. **全部授予**为网关的托管标识（APIM 的，或 LiteLLM 网关的）在每个 Foundry 资源上分配
   *Cognitive Services OpenAI User* 角色，这样网关无需 API 密钥即可调用你的模型。这需要一个能在这些资源上分配
   角色的角色（*所有者*、*用户访问管理员*或*基于角色的访问控制管理员*）。如果没有，顶栏的**权限检查**会给出一份
   可以直接发给 Azure 管理员的申请。在 APIM 上，如果服务的托管标识未启用，设置会先启用它。
2. **创建测试密钥**会获取一个只能调用此后端池的密钥：
   - **APIM：** 一个只能访问此后端池 API 的订阅密钥。
   - **LiteLLM：** 一个只能调用此后端池模型名的虚拟密钥。LiteLLM 只保存它的哈希，所以如果想保留，请现在复制：
     它只显示这一次。上方的**添加别名**可以让已经发送其他模型名（例如 `gpt-5.6-luna`）的客户端也调用到此后端池。

![调用测试选项卡：终结点与模型名、调用前检查，以及获取虚拟密钥和“创建测试密钥”](images/onboarding-6-grant-and-key.png)

## 7. 发送测试请求

点击**发送请求**。请求会经过网关和后端池，你会得到模型的回答以及处理请求的API节点（在 LiteLLM 上，还有它之前在
其他API节点上尝试了几次）。如果刚刚授予访问权限，Azure 最多可能需要 10 分钟才能使角色生效；在此之前页面会解释错误
原因，请稍后重试。

![在这里测试：200，调用成功，由后端池的主API节点 pool-aoai-westus3-gpt-5-6-luna-dz 响应](images/onboarding-7-test-call.png)

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

## 对后端池进行性能测试（可选）

**在这里测试**下方的**性能测试**模拟多个用户同时调用后端池：每个用户发送请求、等待响应后再发下一个。可以选择
**快速检查**（2 个用户、30 秒）、**典型负载**（10 个用户、2 分钟）或**压力测试**（30 个用户、3 分钟），也可以在
**高级设置**中设置用户数、持续时间、逐步加压时间、速率上限、提示长度、输出令牌数和流式传输。页面会先估算请求数
和令牌数；每个请求都像真实流量一样由 Foundry 计费，也会占用部署的速率限制。运行期间可以看到吞吐量、成功率、
p50/p95/p99 延迟、首令牌时间、429 和故障转移、每秒图表以及响应的API节点；结束后给出一句结论（例如所有API节点同时返回
429，说明后端池需要更多容量），并可以**导出 JSON / CSV**，或复制在终端中运行同样测试的命令。每次测试最多 50 个
用户、10 分钟、5,000 个请求，同一时间只能运行一个，并且只有所有者可以启动。

在 LiteLLM 上，它使用第 1 步的虚拟密钥并发送 `max_tokens`；API节点来自 LiteLLM 的响应头
`x-litellm-model-id`，故障转移来自 `x-litellm-attempted-fallbacks`。在 APIM 上，它用订阅密钥和
`api-version` 调用上面选择的 API，并发送 `max_completion_tokens`：APIM 按原样转发请求正文，较新的模型不接受
`max_tokens`。APIM 不报告由哪个API节点响应，所以表格显示响应的区域（Azure OpenAI 的 `x-ms-region`，同一区域的
API节点合并统计），APIM 在 429 后的重试也不会显示为故障转移。

在终端中运行（可用于此网关或任何兼容 OpenAI 的终结点；密钥放在环境变量中，不会留在命令历史里）：

```powershell
$env:LITELLM_KEY = "<your-virtual-key>"
backend\.venv\Scripts\python scripts\benchmark.py --url https://<your-gateway>/v1 --model pool-gpt-5-6-luna --preset typical
#   --users 20 --duration 120 --ramp-up 10 --rate 15 --prompt long --max-tokens 256 --no-stream
#   --json result.json --csv requests.csv        （macOS/Linux：backend/.venv/bin/python，export LITELLM_KEY=...）

# APIM：API 的 URL，密钥放在 Ocp-Apim-Subscription-Key 中，api-version 为 2025-04-01-preview
$env:APIM_KEY = "<your-subscription-key>"
backend\.venv\Scripts\python scripts\benchmark.py --gateway apim --url https://<your-apim>.azure-api.net/<api path> --model gpt-5.6-luna
#   --api-version <version> --key-header <header>   （API 使用其他值时）
```

它每秒打印一行状态，结束时打印报告；按 Ctrl+C 会停止测试（已发送的请求会完成），并仍然打印报告。`--i-know`
只在终端中解除上述限制。

## 8. 观察运行情况

**指标监控**选项卡显示请求数、可用性、返回给客户端的 429（在 LiteLLM 上，还有故障转移挽回了多少）和延迟，以及
每个API节点承担的流量份额和令牌数。**路由预览**可以模拟API节点故障，让你在真正发生之前看到故障转移的过程。

![指标监控选项卡：请求数、可用性、429、延迟和每个API节点的流量份额](images/onboarding-8-metrics.png)

就这些。其他功能（更多后端池、权重、会话感知或别名、绑定你自己的 API、自定义终结点、清理）都在同一个界面中；
[功能一览](features.zh-CN.md#功能一览)列出了全部功能。

> [!CAUTION]
> 应用以**你**的身份操作 Azure，作用于你**真实**的网关（对于 LiteLLM，还会使用它的主密钥）。所有更改对所有客户端
> 立即生效。

## 如果遇到问题

- 顶栏的**权限检查**会重新运行设置检查。凡是你自己的 Azure 权限无法修复的问题，都会变成一份发给管理员的申请：
  角色、作用域、一条 `az` 命令和一个门户链接。
- `python scripts/doctor.py` 会说明你的电脑或网络有什么问题（Python 版本、代理、公司 TLS 证书、DNS），
  以及在你的操作系统上如何修复。
- 在代理后面或受限网络中？请参阅[网络代理与证书](development.zh-CN.md#网络代理与证书)。

## 不用 Azure 试用

在克隆的仓库中，`scripts/demo_server.py` 会让同一个界面连接一个内存中的 Azure、APIM 和
LiteLLM 网关，带有示例部署，本指南中的截图就是这样制作的。它不会访问 Azure。它需要后端的虚拟环境（启动脚本会创建）
和构建好的界面（Node.js 22+）：

```sh
cd frontend && npm ci && npm run build && cd ..
backend/.venv/Scripts/python scripts/demo_server.py    # macOS 和 Linux 上为 bin/python；http://127.0.0.1:8010
```

它像首次运行一样从未登录状态开始。`POST /demo/stage/{sign-in,ready,pool}?product=apim`（或 `litellm`）可以跳到
引导流程中的某个阶段（`pool` 是一个已在使用、带有一小时指标的后端池）；`--product` 和 `--stage` 决定它从哪里开始。
