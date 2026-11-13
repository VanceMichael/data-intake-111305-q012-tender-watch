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
