[← SteadyApiPool](../README.zh-CN.md)

[English](features.md) | **简体中文**

# 功能与工作原理

应用做什么、后端池如何对应到每种网关，以及部署 LiteLLM 网关会创建什么。要开始使用，请参阅
[分步指南](getting-started.zh-CN.md)。

## 功能一览

| 功能 | 说明 |
| --- | --- |
| **设置向导** | 四个步骤：**登录**、网关（**AI 网关**或 **APIM 服务**）、**权限**和**网关 → Foundry**。它检查登录、网关（对于 LiteLLM：可访问、数据库已连接、主密钥有效）、网关的托管标识、你对网关的访问权限，以及网关对 Foundry 的访问权限。能用你的 Azure 权限修复的就直接修复，其余的生成一份可以直接发给管理员的申请。在本地还可以选择网关类型，并选择、新建、连接或删除网关。 |
| **网关选择** | APIM：已有的服务，或新建一个。LiteLLM：按峰值流量确定网关规模并推荐容器应用或 AKS，可一步部署到容器应用，或新建 AKS 群集并在其上安装带 HTTPS 的 LiteLLM，或为已有群集生成部署文件，或连接任何 LiteLLM 代理。 |
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
| **性能测试** | 在后端池的调用指南中或用 `scripts/benchmark.py` 模拟多个用户调用后端池：吞吐量、延迟和首令牌时间的百分位数、429 以及响应的成员（在 APIM 上通过绑定的 API 调用，显示响应的区域），LiteLLM 上还有故障转移，并给出结论，可以导出结果。 |
| **指标监控** | 后端池的请求数、可用性、429 和延迟（来自 APIM 的 Azure Monitor 指标，或 LiteLLM 网关在 Application Insights 中的 OpenTelemetry 跟踪），以及来自 Azure Monitor 的每个成员的流量份额、令牌数和延迟。 |
| **费用** | 网关资源本月的花费和标价。点击顶栏中的网关名称打开；在 LiteLLM 上，那里还有 LiteLLM 自带管理界面的链接。 |
| **同一模型** | 如果后端池中的 Foundry 成员提供不同的模型，则拒绝保存。模型从 Azure 读取，而不是根据部署名称猜测。 |
| **池配额** | 在后端池的成员下方显示其 Foundry 部署合计每分钟允许的 token 和请求数（来自每个部署自己的 `rateLimits`，否则按容量每单位 1,000 TPM、6 RPM 估算）。在 LiteLLM 上，低于网关确定规模时的流量会给出警告。 |
| **调整规模（LiteLLM）** | 网关面板中的**按新的流量调整规模**会为本应用部署的网关按新的峰值重新计算规模，列出每项更改，再以你自己的 Azure 权限应用：容器应用上的副本数、副本大小和缩放触发；AKS 上的节点范围和 LiteLLM 的 Pod；数据库只在你勾选时调整。它会提示可能影响生产流量，并等待你确认；无法就地更改的内容会给出命令。 |
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
- 有多个副本时，冷却状态只能通过 Redis 共享；规模计算器会为每个多于一个副本的规模加上 Redis。

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
| 容器应用 `<name>` | LiteLLM `v1.106.4`（固定映像 `ghcr.io/berriai/litellm:v1.106.4`），端口 4000，通过外部 HTTPS 入口公开，另有一个 OpenTelemetry Collector 边车容器把模型调用的跟踪发送到 Application Insights。副本数、副本大小（只有一个副本时为 1 vCPU、2 GiB，否则为 2 vCPU、4 GiB，即 LiteLLM 对单个工作进程的建议）和缩放规则都来自规模计算。入口会结束 240 秒没有输出的请求。主密钥、盐密钥、数据库 URL 和 `config.yaml` 都是容器应用机密 |
| 容器应用环境 `cae-<name>` | 工作负载配置文件环境，LiteLLM 运行在其消耗配置文件上；日志发送到 Log Analytics。之后可以添加带高级入口的专用配置文件（空闲超时最长 30 分钟，而不是 240 秒），它至少需要两个专用节点 |
| 用户分配的托管标识 `id-<name>` | LiteLLM 调用 Foundry 时使用的身份（`AZURE_CLIENT_ID`）；设置向导会在成员帐户上为它分配 *Cognitive Services OpenAI User*。它没有其他角色 |
| Azure Database for PostgreSQL 灵活服务器 | LiteLLM 的模型表、虚拟密钥和花费记录。只有一个副本时为可突发 B1ms，否则为规模所需连接数对应的常规用途规格（D2ds_v5 起）：可突发层在持续负载下会耗尽 CPU 额度。32 GB。LiteLLM 每分钟写入一次花费，每个副本最多保持 10 个连接。公共网络访问，仅允许 Azure 服务 |
| Azure 托管 Redis（多于一个副本时） | 按峰值从 Balanced B0 到 B5，让各副本共享冷却状态 |
| Log Analytics 工作区 `log-<name>`、Application Insights `appi-<name>` | 容器日志，以及指标监控选项卡所用的跟踪 |

网关的机密由应用在部署时生成一次，之后应用从不轮换：盐密钥用于加密 LiteLLM 保存的成员密钥，因此不能更改。
这也是为什么已存在的网关绝不会被重新部署覆盖。

#### 部署之前，以及部署仍然失败时

一次部署会同时创建多个资源；以前如果 Azure 中途拒绝其中一个，其余资源就会留下来并继续计费。现在：

- **先检查区域。** 填写表单时，应用以只读方式检查该区域能否容纳网关需要的每个资源：Azure 是否在该区域提供每种
  资源类型；PostgreSQL 灵活服务器（订阅在该区域的限制、服务器版本、所选规模的 SKU 以及它的 vCore 配额）；
  是否还能再创建一个容器应用环境；在 AKS 上还检查节点大小、其可用性区域和 vCPU 配额。所有问题一次列出，并给出
  能够部署的区域；在区域可用之前，**部署**按钮保持禁用。
- **开始之前由 Azure 验证部署。** 点击**部署**后，应用先请 ARM 按实际要发送的内容验证这次部署（由各资源提供程序
  检查大小、名称和策略）。验证失败时不会创建任何资源；应用为这次部署新建的空资源组也会被删除。
- **资源仍然失败时停止部署并删除已创建的资源。** 部署需要几分钟，期间 Azure 仍可能容量不足。勾选**如果某个资源
  部署失败，停止部署并删除已创建的资源**（默认勾选）后，一旦某个资源失败，应用会立即停止部署，然后删除这次部署
  创建的资源：只删除带有此网关标记的资源，从不删除资源组。Azure 暂时拒绝删除时（依赖的资源还在，或资源仍在创建中）
  应用会继续重试，并显示已删除的资源和 Azure 仍在删除的资源。部署记录及其错误会保留；Azure 删除完成后可以用同一个
  名称重新部署。清理在应用跟踪部署时进行；如果你关闭了应用，再次打开网关步骤时会继续。取消勾选可以保留这些资源以便
  排查，之后可以用**删除已创建的资源**手动删除。在 AKS 上，这只涵盖 Azure 资源；群集内的安装失败时仍可以
  **重新运行安装**。

**删除网关**（网关面板中的**删除网关**，或网关列表中的**删除**）仅限此应用部署的网关，并需要再次输入名称。它会
删除标记为该网关的资源（先删除容器应用），从不删除资源组。**没有软删除**：Azure 不保留副本，数据库连同其中的
后端池、密钥和花费记录都会丢失。连接的网关绝不会被删除；**断开连接**只会让应用忘记它和它的密钥。

### AKS 上的 LiteLLM 网关

**在 AKS 上部署**会创建以下内容（`infra/gateway/litellm-aks.bicep`，编译为
`backend/pool_manager/templates/litellm-aks.json`，随后是 `litellm/gateway_template.py` 中 `aks_bootstrap` 的
安装）。Azure 资源的标记与容器应用网关相同。

| 资源 | 说明 |
| --- | --- |
| AKS 群集 `<名称>` | Microsoft Entra ID 与 Azure RBAC（无本地帐户），OIDC 颁发者和工作负载标识，Azure CNI 覆盖网络，Azure Linux 节点。节点大小和自动缩放范围、层（区域冗余时为标准层）、出站 IP 以及每个节点的 SNAT 端口都来自规模计算：每个对 Foundry 的并发调用都占用一个端口，默认每个节点 1,024 个。入口使用托管 Gateway API 和应用程序路由的 Istio 实现（托管 NGINX 入口已停用）。补丁版本和节点映像自动升级 |
| 群集中的 `litellm` 命名空间 | LiteLLM 的 Helm 图表（固定为 `1.106.4`，使用工作负载标识，每个 Pod 1 vCPU、4 GiB，数量按规模计算，按 CPU 伸缩）、把模型调用跟踪发送到 Application Insights 的 OpenTelemetry Collector，以及位于公共 IP 上、DNS 名称为 `<名称>.<区域>.cloudapp.azure.com` 的网关。cert-manager（位于 `cert-manager`）获取并续订 Let's Encrypt 证书。纯 HTTP 只会重定向到 HTTPS |
| 用户分配的托管标识 `id-<名称>` | 与 LiteLLM 的服务帐户联合；LiteLLM 以它的身份调用 Foundry。设置向导会在成员帐户上为它授予 *Cognitive Services OpenAI User* |
| PostgreSQL、Redis（多于一个 Pod 时）、Log Analytics、Application Insights | 与容器应用相同 |
| 角色分配 | 你在该群集上的 *Azure Kubernetes 服务 RBAC 群集管理员* |

机密（主密钥、盐密钥、Postgres 密码）由应用生成，安装完成前保存在你的电脑上；它们只通过运行命令的文件作为
Kubernetes 机密进入群集，安装过程从不打印它们。之后应用只保留加密的主密钥，与连接的网关相同。

**群集由你维护。** 本应用只管理 LiteLLM 中的后端池、别名和密钥；只有当你在网关面板中调整规模时（**按新的流量调整规模**：
节点范围，以及通过运行命令中的 `helm upgrade --reuse-values` 调整 LiteLLM 的 Pod，事先提示并经你确认）才会更改群集。
网关面板列出了可直接复制的命令，并链接到 Azure 门户中的群集：

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
