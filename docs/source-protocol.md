# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `registry`、`repository`、`reference`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

- `tag_moves`：闭包抓取期间标签变化；业务侧关注继续冻结摘要而非追随新标签。
- `descriptor_closure`：index已收到但平台manifest或blob缺失；业务侧关注准确说明闭包缺口不虚报交付齐全。
- `referrer_subject`：关联制品subject不对应验收对象；业务侧关注拒绝串入其他对象的交付资料。

另外提供 `/v2/vendor/tool/manifests/accepted`、`/v2/vendor/tool/manifests/sha256:8990dbed525923654e48c2bffe37d2dabcdc0ce7f159ef8ba50e2c0c93d9d3a6`、`/v2/vendor/tool/manifests/sha256:f0eafcb032ddecd3fef89fe1dc16bbd59789b9a38c46e4e9700f1c4dc5c7b65d`、`/v2/vendor/tool/blobs/sha256:9d99a75171aea000c711b34c0e5e3f28d3d537dd99d110eafbfbc2bd8e52c2bf`、`/v2/vendor/tool/blobs/sha256:c0705df23e6561c1b1de5fe226612b047d7e228c1dd6c0c245850060554f4f3c`、`/v2/vendor/tool/referrers/sha256:f0eafcb032ddecd3fef89fe1dc16bbd59789b9a38c46e4e9700f1c4dc5c7b65d`、`/v2/vendor/tool/manifests/sha256-f0eafcb032ddecd3fef89fe1dc16bbd59789b9a38c46e4e9700f1c4dc5c7b65d`。响应内容和版本由场景文件逐项声明，便于核对实际收到的来源资料。

## 领域接入契约

来源实现OCI Distribution只读端点，媒体类型和描述符沿用OCI标准。示例layer是不可执行合成字节，仅用于内容校验。referrers返回404时表示演练源不支持该API，不等同于空列表；兼容tag索引单独声明。对镜像不执行、不解包、不进行安全攻击或漏洞复现。

## 交付验收模型

验收器（`tender_watch.acceptance`）在只读接口上执行一次独立验收，结论写入与原始响应同一个 SQLite 归档，只追加、不覆盖。

### 标签冻结

一次验收从 `GET /v2/<仓库>/manifests/<引用>` 开始。标签引用在第一次观察时立即冻结为响应字节的 sha256 摘要；之后所有清单与 blob 一律按摘要寻址，标签在抓取期间移动不会改写这次冻结视图。恢复（`resume`）只读取台账中固化的摘要与策略，不重新解析标签。摘要引用若返回不同内容直接拒收。

### 描述符闭包

多平台 index 只代表“看到了成员描述符”，不代表每个平台 manifest 已收到。验收器沿 `index_member`、`config`、`layer`、`referrer` 逐边展开，每个描述符的 `mediaType`、`size`、`digest` 与实际收到的字节三要素核对：摘要不符或长度不符判为 `rejected`；来源 404 判为 `missing`，闭包状态为 `incomplete` 而非拒收；Content-Type 与描述符媒体类型不一致保留为可追查警告。相同字节按摘要只存一份（`bodies` 复用），但每次来源观察（路径、URL、状态、头部、时间）与每条引用边分别留痕，仓库来源不丢。

### 平台选择

本地策略可声明 `os/architecture[/variant]` 列表。未选中的平台不抓取，但描述符仍登记为 `skipped` 并保留 `index_member` 边；无平台声明的 index 成员（如证明清单）默认领取。缺省策略领取 index 声明的全部平台。

### 关联制品与可见性

对冻结根摘要请求 `GET /v2/<仓库>/referrers/<摘要>`：200 表示接口可用并领取索引中的关联制品；404 只证明接口不支持（`api_unsupported`），与“确实没有关联制品”是两件事。是否回退到 `sha256-<hex>` 兼容标签索引由本地策略 `allow_fallback_referrers` 决定；兼容标签也不存在时，才在当前可见范围内按无关联制品处理。关联清单的 `subject.digest` 必须等于被验收的根摘要，否则判 `subject_mismatch` 拒收，防止串入其他对象的交付资料。策略可要求指定 `artifactType` 必须通过验收，或要求关联制品可见性必须确定。

### 判定与追查

运行状态为 `complete`（闭包全部 verified/skipped）、`incomplete`（仍有 missing/pending）、`rejected`（内容或引用关系非法、策略性要求未满足）或 `failed`（标签无法冻结）。报告（`accept-report`）逐节点给出状态与原因码，逐描述符给出全部来源观察（含失败的 404 与重试的 200），验收组可据此查出缺哪个描述符、为何拒收、最终内容来自哪个仓库 URL。

