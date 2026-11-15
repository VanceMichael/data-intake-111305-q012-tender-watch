# 招标软件交付目录的 OCI 描述符闭包采集

这是资料接收程序。基础部分请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。验收部分按 OCI Distribution 只读接口收集交付元数据：冻结一次标签观察得到的摘要，再沿描述符领取所选平台及关联制品，给出可追责的闭包视图。当前不生成面向业务的有效版本，也不自动执行门户提交；重复观察分别保留，原始内容按摘要复用但来源与引用关系不丢。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。程序不启动容器、不执行或解包下载内容、不扫描漏洞；合成 layer/blob 均为不可执行字节。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q tender_watch tests tools
python3 -m tender_watch demo
```

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取及归档，并输出原样记录；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m tender_watch serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m tender_watch capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释可与原始接收独立演进。

## OCI 交付物验收

```sh
python3 -m tender_watch accept \
  --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 \
  --source local-source --registry delivery-local --repository vendor/tool \
  --reference accepted \
  --platform linux/amd64 --platform linux/arm64/v8 \
  --require-artifact-type application/vnd.example.delivery-note.v1+json \
  --referrers fallback --drift-probe
```

退出码：`0` 验收通过，`3` 闭包不完整（有描述符缺失或必备关联制品未到），`4` 确定性拒收，`5` 仍在运行。

### 标签冻结与闭包

- 标签路径 `manifests/<tag>` 只在首次冻结时请求一次，根摘要由实际收到的字节计算；冻结后一切引用走 `manifests/<digest>` 与 `blobs/<digest>`。抓取期间标签移动不会改写冻结视图，`--drift-probe` 只在终判后只读复查并记录漂移，不改变结论。
- 多平台 index 收到不等于每个平台 manifest 已收到。`--platform` 选择本次验收的平台（可重复，缺省为 index 内全部平台）；未选平台登记为 `visible_not_required`，其 config/layer 不领取。
- config 与 layer 各自携带媒体类型、长度和摘要，逐描述符校验。长度不符、摘要不符、文档结构与声明媒体类型不符分别给出独立原因码；相同字节在 `bodies` 表复用，但每次观察的 URL、状态码、头部、时间在 `captures` 表各自留痕。
- 带外部 `urls` 的描述符只记录 `external_url_not_followed`，不跨仓库/外网跟随。

### 关联制品（referrers）

- `GET /v2/<repo>/referrers/<digest>` 返回 200 空列表表示“确实没有关联制品”；返回 404（UNSUPPORTED）表示接口缺失，两者在报告的 `referrers_state` 中分别为 `supported` 与 `unsupported`。
- `--referrers` 是本地策略：`api` 只用标准接口；`fallback` 在接口缺失时按 OCI 兼容 tag `sha256-<hex>` 读取兼容索引；`skip` 显式跳过。
- 每个关联制品必须取回 manifest 本体并核验 `subject.digest` 与被验收的冻结摘要一致；`subject` 缺失或指向其他对象一律拒收，且不继续领取其 blob。`--require-artifact-type` 声明必备类型，缺失时结论为不完整。

### 恢复与可追责视图

失败后用 `--resume <验收单ID>` 恢复：标签路径永不重访，只针对冻结描述符重试缺口，因此标签即便已移动也不会跟到新内容。报告中每个节点给出 `state`（present/missing/rejected/visible_not_required）、`reasons` 原因码、所属仓库与 `provenance`（来源 capture 的 URL、状态码、时间、实际内容摘要），`missing`/`rejected` 直接列出描述符摘要，`edges` 保存 platform/config/layer/referrer 引用关系。验收单、节点、边与事件表与原始 `captures`/`bodies` 共库存放，见 `acceptance.py`；协议原语在 `oci.py`。

## 演练场景

`fixtures/oci/` 下六个场景由 `tools/generate_oci_fixtures.py` 离线生成（摘要与长度均实算），覆盖标签移动、闭包缺口、referrers 支持/空列表/兼容 tag 回退、subject 串用拒收。用 `serve --scenario fixtures/oci/<场景>.json` 指定；找不到指定文件直接报错，不回退默认资料。

## 开发资料

来源协议见 `docs/source-protocol.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `tender_watch` 和命名空间内的合成来源 JSON（含 `fixtures/oci/` 场景）。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m tender_watch demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
