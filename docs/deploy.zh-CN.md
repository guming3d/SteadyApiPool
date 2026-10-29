[← SteadyApiPool](../README.zh-CN.md)

[English](deploy.md) | **简体中文**

# 托管到 Azure

除了在你的电脑上运行，还有两种方式把应用本身运行在 Azure 中：在 App Service 上为团队服务、位于一个网关之前，
或作为公开的多租户网站（仅 APIM）。

## 部署到 Azure

这会把管理器本身托管在 App Service 上，使用 Entra 登录（Easy Auth），供团队管理一个网关：

- **APIM：** 一个已有的服务；如果你没有指定服务，部署会在其资源组中新建一个。
- **LiteLLM：** 一个**已有的**网关。Bicep 从不创建或更改它，因为它的机密由应用生成一次，重新预配不能轮换它们。
  所以请先用本地应用部署网关（[快速开始](getting-started.zh-CN.md)第 1–3 步），或部署在 AKS 上。

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
| APIM：API 管理服务 `apim-pool-*`（仅在未设置 `APIM_NAME` 时） | 默认为 `BasicV2`（`APIM_SKU`），带有供API节点使用的系统分配托管标识，并标记 `managed-by: apim-backend-pool`。从创建起开始计费 |
| APIM：服务上的自定义角色 *APIM Backend Pool Manager* | 用于指标监控选项卡的 `Insights/metrics/read`、`backends/*`、`namedValues/*`、`apis/read,write,delete`（只在删除后端池时删除它自己创建的 API）、`apis/operations/write` 和 `apis/policies/read,write`，以及让设置检查应用自身访问权限的 `Authorization/permissions/read` |
| LiteLLM：网关的容器应用及其 Application Insights 组件上的自定义角色 *AI Gateway Pool Manager*（仅容器应用上的网关） | `Microsoft.App/containerApps/read` 和 `listSecrets/action`（网关的 URL、标识和主密钥；之后每次后端池更改都通过 LiteLLM 的管理 API 进行），用于指标监控选项卡的 `Insights/components/read` 和 `Insights/logs/*/read`，以及让设置检查应用自身访问权限的 `Authorization/permissions/read`。它不能更改容器应用、数据库或其他任何内容 |
| 每个发现订阅上的自定义角色 *APIM Backend Pool Deployment Reader* 或 *AI Gateway Pool Deployment Reader* | `CognitiveServices/accounts/read` 和 `accounts/deployments/read`，用于API节点指标的 `Insights/metrics/read`，以及只读的 `Authorization/permissions`、`roleAssignments` 和 `roleDefinitions`，让设置可以检查网关在每个帐户上的角色。它没有 `listKeys`，也不能更改角色。用 Bicep 参数 `enableDeploymentDiscovery=false` 关闭，用 `discoverySubscriptionIds` 选择订阅（默认为网关所在订阅） |

网关自身的标识需要每个API节点帐户上的 *Cognitive Services OpenAI User*（或任何允许推理的角色）。设置向导会检查这一点，
并在启用委托的 Azure 访问时授予。在 APIM 上，如果API节点的机密引用 Key Vault，它的标识还需要该保管库上的
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
| `POOL_MANAGER_ID_PREFIX` | `pool-` | 新后端池、API节点和测试密钥的前缀 |
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
该服务和订阅中的内容（例如后端池API节点的 URL 和 API 策略）对任何拿到网址的人可见，所以请使用演示用的服务。

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
