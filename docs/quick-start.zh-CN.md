[← SteadyApiPool](../README.zh-CN.md)

[English](quick-start.md) | **简体中文**

# 快速开始

从登录到第一次通过后端池调用，每一步都有截图。它接着 README 中的
[1. 下载并启动](../README.zh-CN.md#1-下载并启动)，此时应用已在 **http://127.0.0.1:8000** 运行。
每一步的所有选项都在[分步指南](getting-started.zh-CN.md)中。

> [!NOTE]
> 截图为英文界面。浏览器语言为中文时界面显示中文，也可以用顶栏的语言按钮切换。下文使用中文界面上的按钮名称。

> [!CAUTION]
> 应用以**你**的身份操作 Azure，作用于你**真实**的网关（对于 LiteLLM，还会使用它的主密钥）。所有更改对所有客户端
> 立即生效。

## 2. 登录 Azure

点击**用浏览器登录**；在没有浏览器的机器上点击**改用验证码登录**。应用会记住登录状态。

![设置页的登录、AI 网关、权限和网关 → Foundry 四步中的第 1 步：用浏览器登录，或改用验证码登录](images/onboarding-1-sign-in.png)

## 3. 选择网关

选择 **LiteLLM 网关**或 **Azure API 管理**（之后也可以切换），然后在已有的网关上点击**使用此网关**或**使用此服务**。

![设置页第 2 步：选择 LiteLLM 网关或 Azure API 管理，以及订阅中的 LiteLLM 网关，每个都有“使用此网关”按钮，另有“部署网关”和“连接已有网关”](images/onboarding-2-choose-gateway.png)

还没有 APIM 服务？**新建 APIM 服务**会填好所有内容（基本 v2 层），并等到服务就绪，通常需要几分钟。

![设置页第 2 步，选择了 Azure API 管理：订阅中的 APIM 服务，每个都有“使用此服务”按钮，另有“新建 APIM 服务”](images/onboarding-2-choose-apim.png)

![新建 APIM 服务表单，已预填基本 v2 层](images/onboarding-2b-create-apim.png)

还没有 LiteLLM 网关？**部署网关**会按你的峰值流量确定网关规模，推荐 Azure 容器应用或 AKS 并说明计算过程，然后
完成部署（5–25 分钟）。规模背后的数字见[确定网关规模](getting-started.zh-CN.md#确定网关规模)；如果你已在运行
LiteLLM 代理，点击**连接已有网关**。

![按你的流量确定网关规模：峰值 RPM、token、超时和流式传输，推荐容器应用的规模并展开了计算过程，旁边是 AKS 的规模](images/onboarding-2b-deploy-gateway.png)

## 4. 选择要放进后端池的部署

打开**模型部署**选项卡，点击**用此模型创建后端池**。还没有任何部署？**部署模型**会帮你创建。

![模型部署选项卡：gpt-5.6-luna 部署在三个区域，带有“用此模型创建后端池”按钮](images/onboarding-3-deployments.png)

## 5. 发布后端池

后端池打开时所有内容都已填好。**故障转移**把所有流量发给主部署，在它失败时切换到备用部署；**轮询**和**加权**
则把流量分摊到各个成员。

![添加后端池，已预填故障转移模式下的三个成员和冷却设置](images/onboarding-4-create-pool.png)

点击**发布到 APIM**（或**发布到网关**），查看应用将写入的全部内容，然后确认。

![确认发布：新的后端池及其三个成员](images/onboarding-5-publish.png)

## 6. 允许网关调用你的模型，并获取密钥

在**调用测试**选项卡上，**全部授予**让网关的托管标识无需 API 密钥即可调用你的 Foundry 资源，**创建测试密钥**
会获取一个只能调用此后端池的密钥。

![调用测试选项卡：终结点与模型名、调用前检查，以及获取虚拟密钥和“创建测试密钥”](images/onboarding-6-grant-and-key.png)

## 7. 发送测试请求

点击**发送请求**，得到模型的回答以及处理请求的成员。页面下方的**发送请求**部分提供 curl、Python、JavaScript 和
PowerShell 的客户端代码。

![在这里测试：200，调用成功，由后端池的主成员 pool-aoai-westus3-gpt-5-6-luna-dz 响应](images/onboarding-7-test-call.png)

## 8. 观察运行情况

**指标监控**选项卡显示请求数、可用性、429 和延迟，以及每个成员承担的流量份额。**路由预览**可以模拟成员故障。

![指标监控选项卡：请求数、可用性、429、延迟和每个成员的流量份额](images/onboarding-8-metrics.png)

## 如果遇到问题

顶栏的**权限检查**会重新运行设置检查，并为你的权限无法修复的问题写好一份发给 Azure 管理员的申请；
`python scripts/doctor.py` 会说明电脑和网络的问题。更多内容见[如果遇到问题](getting-started.zh-CN.md#如果遇到问题)。
