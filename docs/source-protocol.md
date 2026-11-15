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

## 验收演练场景（fixtures/oci/）

场景由 `tools/generate_oci_fixtures.py` 离线生成，所有 digest/size 由实际字节计算，仓库固定为 `vendor/tool`，验收标签固定为 `accepted`。

- `tag_moves.json`：标签首次返回单平台 index，第二次返回多平台 index。验收程序必须冻结首次摘要；`--drift-probe` 记录的 `tag_drift_observed.moved` 为真，但冻结视图与结论不变。
- `closure_gap.json`：index 声明两个平台，arm64 平台 manifest（及其 config/layer）路由缺失返回 404。报告 `missing` 列出具体描述符，节点原因码为 `http_error_status`，状态为 `incomplete`，不虚报齐全。
- `referrers_supported.json`：referrers API 返回 200 与两个关联制品（交付说明、签名），subject 均指向被验收根摘要；未被 `--require-artifact-type` 要求的制品标记为 `visible_not_required`。
- `referrers_empty.json`：referrers API 返回 200 空列表，`referrers_state=supported`，与接口缺失的 404 明确区分。
- `referrers_fallback.json`：referrers API 返回 404 UNSUPPORTED；仅当本地策略为 `--referrers fallback` 时读取兼容 tag `sha256-<hex>` 索引，`referrers_state=fallback_index`。
- `referrer_subject_mismatch.json`：关联制品 manifest 的 `subject.digest` 指向其他对象，节点状态为 `rejected`，原因码 `subject_mismatch`，其 blob 不进入闭包。

## 验收持久化模型

原始 HTTP 交换仍写入 `captures`/`bodies`（字节按摘要复用，观察各自留痕）。验收视图另用四张表：`acceptances`（验收单、冻结根摘要、referrers 状态、本地策略）、`acceptance_nodes`（描述符角色、必填性、状态、原因与来源 capture）、`acceptance_edges`（platform/config/layer/referrer 引用关系）、`acceptance_events`（标签冻结、漂移、缺口、拒收等有序事件）。恢复运行只重试冻结描述符的缺口，不重新请求标签路径；已通过（accepted）的验收单恢复时只重放视图，不发起请求。
