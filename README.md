# 招标软件交付目录的 OCI 描述符闭包采集

这是资料接收程序的基础版。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。当前不生成面向业务的有效版本，也不自动执行门户提交；重复观察会分别保留，原始内容按摘要复用。

在此基础上提供 OCI 交付验收能力：按 Distribution 只读接口把标签观察冻结为摘要，沿描述符闭包领取所选平台的 manifest、config、layer 及要求的关联制品，逐节点核对媒体类型、长度与摘要，并输出可审计的验收报告。全程只处理合成 JSON 与不可执行字节，不启动容器、不执行下载内容、不做漏洞扫描。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q tender_watch tests
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

## 交付验收

`registry.py` 封装 OCI Distribution 只读路径（manifests、blobs、referrers），`acceptance.py` 执行一次验收并把节点、引用边、问题与来源台账写入同一个归档库。标签在首次观察即冻结为摘要，抓取中标签移动或失败后恢复都不会追随新标签。

```sh
python3 -m tender_watch accept \
  --base-url http://127.0.0.1:实际端口 --archive .runtime/accept.sqlite3 \
  --source local-source --repository vendor/tool --reference accepted \
  --platform linux/amd64 --fallback-referrers \
  --require-artifact application/vnd.example.delivery-note.v1+json
```

- `--platform os/architecture[/variant]` 可重复，缺省领取 index 声明的全部平台；未选平台保留描述符登记但不抓取。
- `--fallback-referrers` 允许在 referrers 接口返回 404 时使用 `sha256-<hex>` 兼容标签索引；不加该开关时 404 只记录“接口不可见”，不等同于空列表。
- 退出码 0 为 `complete`，1 为 `incomplete`/`rejected`，2 为标签无法冻结。
- `resume --run-id` 按冻结视图恢复未完成验收，策略从台账读取；`accept-report --run-id` 输出完整闭包、问题码与每个描述符的来源观察。
- `demo-acceptance` 用 `fixtures/acceptance.json` 端到端演示多平台闭包、标签移动冻结、关联制品 subject 校验与兼容索引；该场景由 `tools/build_acceptance_fixture.py` 生成。

## 开发资料

来源协议见 `docs/source-protocol.md`，领域观察关系见 `tests/test_source_contract.py`，验收闭包契约见 `tests/test_acceptance.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径；`fixtures/acceptance.json` 是对应的 OCI 验收演练场景（标签移动、闭包缺口、关联制品 subject）。基础版测试验证的是原始采集和协议，不代表业务处理已经具备这些能力。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `tender_watch` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m tender_watch demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
