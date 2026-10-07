# Extore 预设商品处理器

这个仓库提供可以直接运行的开源商品处理器。输入什么、需要怎样填写、输出什么，都写在处理器代码里。Extore 从这些定义生成顾客表单、说明和结果校验规则。

商家选择发布版本内的预设，并填写预设声明的配置。商家不能把一个文件名、Git 地址或上传的 Python 文件当作处理器。Extore 通过 Git 子模块固定本仓库的提交，升级须审核源码、运行测试，再更新主项目的子模块版本。

MIT 许可，Python 3.11 或更新版本，只使用 Python 标准库，没有运行时依赖。

## 预设

| 预设 ID | 顾客填写 | 商家配置 | 成功输出 |
| --- | --- | --- | --- |
| `resource_link` | 无 | `resource_url` 必填；`message` 可选 | `resource_url`、`message` |
| `personalized_text` | `name` 称呼，必填，最多 200 字符 | `template` 纯文本模板 | `content` |
| `csv_summary` | `csv_text` CSV 文本、`value_column` 数值列名；`group_column` 分组列名可选 | `invalid_policy`：`reject`（默认）或 `skip` | `report` 统计说明、`summary` JSON 字符串 |
| `json_formatter` | `json_text` 完整 JSON | `indent`：`2`（默认）、`4` 或 `compact`；`sort_keys`：`no`（默认）或 `yes` | `formatted_json`、`report` |
| `text_cleanup` | `text` 每行一条的文本 | `trim_lines`、`remove_blank_lines` 默认 `yes`；`deduplicate`：`exact`（默认）、`casefold` 或 `none` | `cleaned_text`、`report` |
| `document_template` | `title` 标题、`body` 正文；`name` 称呼可选 | `template` 文本模板；`output_format`：`markdown`（默认）或 `plain` | `content`、`format` |

这四个处理器分别用于实验 CSV 的描述统计、API 配置的 JSON 校验、条目名单去重、项目或交付说明模板。输入是粘贴的文本，结果是可复制的文本、Markdown 或 JSON；不读取附件、不联网、不调用 AI，不生成 DOCX/PPTX 文件。

`resource_link` 返回商家配置的 HTTPS 资源地址和使用说明，不访问资源地址。地址可包含访问令牌，按秘密配置处理；说明为普通文本，店主可回读编辑。二者都不会通过公开商品资料提前提供。使用说明按多行纯文本交付。

`personalized_text` 使用 `string.Template` 做文本替换，只接受 `$name`、`${name}` 和 `$$`（一个美元符号）。默认模板是：

```text
你好，$name！
你的商品已准备好。
```

模板不执行 Python、HTML、Shell 或其他代码。顾客名字里的占位符也不会再次替换。

### 可直接运行的场景

在本仓库目录运行以下命令。每个示例文件都是一份完整任务，含顾客 `params` 和商家 `configuration`；不含真实顾客资料或凭据。没有安装本包时也可从本仓库目录执行：

```sh
# 实验记录：按 group 列分别汇总 score 列
python -m extore_processors csv_summary < examples/csv_summary.json

# API 配置：保留数值精度与原有指数写法，按键排序、缩进
python -m extore_processors json_formatter < examples/json_formatter.json

# 项目条目名单：去首尾空白、空行，忽略大小写去重并保留第一次文字
python -m extore_processors text_cleanup < examples/text_cleanup.json

# 交付说明：将标题、称呼与正文填入商家模板
python -m extore_processors document_template < examples/document_template.json
```

stdout 是进度 JSON 行，最后一行的 `kind` 为 `result`、`state` 为 `succeeded`。实际内容位于 `output`；`csv_summary.summary` 本身是字符串，使用时再做一次 JSON 解析。示例可用于主项目打包、隔离执行与结果协议验证；直接运行上述 Python 命令不创建沙箱。

`csv_summary` 只统计所选数值列，分组列可留空。默认 `reject` 遇到不合法数据即拒绝；选择 `skip` 时会在结果中报告被跳过的记录，不把缺失值默认为零。统计是所提供记录的描述，不代替实验设计、统计推断或因果结论。

`json_formatter` 支持任意合法 JSON 顶层值。数字保留原文，例如 `12345678901234567890.123456789` 和 `1.20e-3` 不经过二进制浮点转换；对象键默认保留输入顺序，数组顺序和字符串内容保持不变。重复键、非有限数字、孤立 Unicode 代理项、注释和尾逗号会被拒绝。

`text_cleanup` 先按配置去除首尾空白，再处理空行和重复行，始终保留首次出现的顺序。`casefold` 只改变比较方式，不把交付文字改成小写。CRLF 与 CR 换行为 LF，不进行 Unicode 规范化；所有条目被删除时，`cleaned_text` 可以为空，报告仍显示实际删除数量。

`document_template` 的默认模板如下，空行是实际换行：

```text
# $title

$name

$body
```

它只接受 `$title`、`$name`、`$body`、对应的 `${...}` 和 `$$`，只替换一次。Markdown 格式将标题与称呼作为单行文字转义，正文保留顾客提供的 Markdown；`plain` 按文字填入模板，不会自动移除模板里的 Markdown 符号。正文、网址和占位符都是内容，不执行、不访问，也不会读取工作流环境或秘密。

四个新增处理器的长文本字段最多 10,000 字符；`document_template.title` 和 `name` 各最多 200 字符。商家配置枚举也必须以字符串填写，例如 `"indent": "2"`，不能使用数字 `2`。

| 处理器 | 额外资源限制 |
| --- | --- |
| `csv_summary` | 最多 500 个数据行、1,001 个含空白记录的 CSV 记录、30 列、50 组；表头最多 100 字符、组名 200 字符。数值最多 30 位有效数字，非零绝对值 `1e-30` 至 `1e12`，指数绝对值不超过 30。每个输出字符串最多 100,000 字符。 |
| `json_formatter` | 最多 32 层容器、10,000 个值、每容器 2,000 项；单个数字最多 256 字符、128 位有效数字，指数绝对值不超过 1,000。格式化 JSON 与报告合计最多 100,000 UTF-8 字节。 |
| `text_cleanup` | 最多 10,000 行；清理文本与报告合计最多 100,000 UTF-8 字节。 |
| `document_template` | 模板最多 10,000 字符；生成的 `content` 最多 100,000 UTF-8 字节。 |

上述字段限制还须满足整份任务输入的 200,000 字节限制。成功结果的整条 JSONL 最多 100,000 UTF-8 字节，包括结果信封、所有输出字段和 JSON 转义；超限会拒绝返回成功，因此不能让各字段同时占满上限。服务器默认运行限制为 120 秒墙钟时间、120 秒 CPU、256 MiB 地址空间、1,000,000 字节进程输出，可在本店配置档案允许范围内调整。直接运行 Python API 或下方命令行不会应用 Extore 的隔离与进程资源限制。

队列商品没有这个处理程序，输入和输出由 Extore 内的商品配置定义，处理者可以是商家、拥有商品管理链接的协作者，或者 AI。

## Python API

```python
from extore_processors import catalog, get_spec, run, validate_configuration

specs = catalog()
spec = get_spec("personalized_text")
settings = validate_configuration("personalized_text", {"template": "Hello, $name!"})
result = run("personalized_text", {"name": "Alice"}, settings)
assert result == {"status": "succeeded", "output": {"content": "Hello, Alice!"}}
```

`catalog()` 返回预设规格列表，`get_spec(id)` 返回某个规格。返回值是副本，调用方修改不会影响运行中的定义。`get_processor(id)` 提供带有 `.spec` 和 `.run(params, configuration, context=...)` 的只读处理器对象。不提供 context 时仍返回原有结果字典，不向 stdout 写日志。

每个规格包含：

- `id`：固定预设标识；不能传文件名或模块路径。
- `schema_version`：规格版本，当前为 `1`。
- `name`、`description`：`zh-CN` 与 `en` 双语文本。
- `delivery`：当前预设均为 `content`。
- `parameters`：顾客输入字段。
- `outputs`：成功交付字段。
- `configuration`：商家配置字段；附带 `secret`、`max_length`，可选 `default`。
- `shop_configuration`：同一套代码定义的配置 schema，供店铺配置档案使用。它是独立副本，当前与 `configuration` 兼容。
- `progress_steps`：代码定义的默认步骤；仅在任务尚未定义步骤时初始化。

输入、输出和配置字段都有 `key`、双语 `label`、双语 Markdown `description`、`collapsed`、`required`、`type`。字段类型与 Extore 商品字段兼容。`template` 与 `message` 明确标记 `secret: false`，供店主回读编辑；`resource_url` 为秘密。Extore 对缺失、含糊或重复的声明按秘密处理。公开规格只包含定义和通用默认模板，不能包含商家填写的配置值。

`validate_configuration(id, configuration, allow_incomplete=True)` 可以保存未填写完成的草稿，仍会检查已经填写的 URL、模板和数据类型。发行卡密和实际运行必须使用默认的完整校验。声明的默认值会应用于缺失字段；明确填写空字符串不会被默认值覆盖。

配置和顾客输入只允许代码定义的字段，值必须是字符串。资源地址必须为 HTTPS、有主机名、没有账号密码、空白或控制字符。资源地址最多 2,000 字符，模板和说明最多 10,000 字符，输出最多 100,000 字符。

`validate_parameters(id, params)` 提供与运行时相同的顾客输入校验与规范化，Extore 应在接收任务前调用它，避免必填缺失或超长的输入进入队列后才失败。

失败抛出 `ProcessorError`，带有 `.code` 和可选 `.field`，不会在错误消息中包含顾客输入或私密配置。

## 程序化进度

0.2.0 提供 `ProcessorContext`。步骤按声明顺序排列，`id` 唯一且不可变，显示名称可以使用多种语言。首次定义必须包含 1 至 30 个步骤；已有任务计划不能重新定义。

```python
import json
from extore_processors import ProcessorContext, run

context = ProcessorContext(
    steps=[],
    completed_steps=[],
    shop_context={"shop_id": "shop-a", "profile_id": None, "revision": None},
    emit=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True),
)
result = run(
    "personalized_text", {"name": "Alice"}, {"template": "Hello $name"}, context=context
)
```

预设会先验证参数与配置，再为尚无计划的任务定义「核对信息」「生成交付」两步。校验和结果生成实际完成之后才报告相应步骤，最后返回成功结果。进度消息不会包含顾客输入、资源链接、模板或店铺配置值。

扩展处理器可在代码中调用 `context.define_steps([{ "id": "prepare", "label": {"en": "Prepare", "zh-CN": "准备"} }])`，再通过 `context.progress(message="准备完成", completed_steps=["prepare"])` 标记完成；也可以使用 `context.progress(50, "正在准备")` 更新百分比与消息。完成 ID 必须属于当前计划，不得重复或倒退。

已有预定义计划会保留。若其 ID 与预设的两步相同，预设可以继续更新这些步骤；若不同，预设只报告百分比和消息，不替换计划，也不冒充其他步骤已经完成。

`context.steps` 和 `context.completed_steps` 为只读快照。`context.shop_context` 是不可变 `ShopContext`，只包含服务端提供的 `shop_id`、可选 `profile_id` 和正整数 `revision`，不包含凭据。顾客 `params` 不会覆盖店铺上下文。实际配置值由 Extore 在店铺范围内解析后放入单独的 `configuration`。

`context.environment` 是独立的只读字符串映射，由 Extore 从本张卡密发行时冻结的店铺配置版本读取，包含普通变量和获准供此处理器使用的秘密值。默认是空映射，顾客参数不覆盖它。Extore 同时以 `EXTORE_WORKFLOW_<NAME>` 前缀传入进程环境，避免改变 Python、动态加载器或宿主服务的设置。环境不会自动插入模板、进度或交付输出；处理器代码仍须审查，不能把秘密打印、记录或交付给顾客。

`context.instructions` 是本次执行开始时复制的只读工厂／车间工作提示词，独立于顾客 `params`、处理器 `configuration` 和环境。省略、`null` 或空对象表示没有此上下文，兼容旧版本输入。非空对象严格包含以下字段：

```text
schema: "extore.work-instructions.v1"
shop_id: 本次执行的店铺 ID
product_id: 本次执行的商品 ID
factory_slogan: 工厂提示词（最多 4,000 字符）
workshop_slogan: 车间提示词（最多 4,000 字符）
revision: 服务端生成的 64 位小写 SHA-256 十六进制摘要
```

非空上下文需要额外传入可信的顶层 `product_id`，并保留 `shop_context.shop_id`；处理器分别比对两者，不能从提示词对象本身取得验证范围。直接使用 Python API 时同样传入 `ProcessorContext(product_id=product_id, shop_context=shop_context, instructions=instructions)`。上下文只保存文本；预设不会执行其中的代码、替换模板、覆盖顾客输入，或把它们自动加入日志、进度和交付。

摘要由除 `revision` 外的五个字段生成：`json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")`，再计算 SHA-256。它用于检查快照一致性，不代表身份认证或额外权限。已构造的上下文不会因商家之后修改提示词而改变；下一次执行可取得新快照。

## 单任务命令行协议

### 卡密属性与修改轮次

0.3.0 的 `ProcessorContext` 可以读取服务端传来的卡密属性、剩余权益与本次修改建议。它们都在构造时复制并冻结，顾客参数不能覆盖这些值：

| 属性 | 内容 |
| --- | --- |
| `job_id`、`attempt` | 可信任务 ID、当前执行尝试；旧输入没有提供时为 `None` |
| `card_attributes` | 发行时冻结的自定义标量属性，最多 20 项；属性名称由商家定义 |
| `entitlements` | `None`，或 `attribute_key`、多语言 `label`、`total`、`used`、`remaining`、`can_request`、`reason` |
| `revision` | `current` 修改轮次、`message` 修改建议、`is_revision`；首次交付为第 0 轮 |
| `last_delivery` | 最近成功交付的 `revision`、`attempt`、`created`，或 `None`；没有正文或文件内容 |
| `deliveries` | 可选的历史版本元数据；每项额外有 `revealed`、`has_files`。执行端通常省略整个历史列表，默认空元组 |

例如，商家可以把 `edit_passes` 作为修改额度属性，也可以使用其他名称；本包没有内置任何特定权益属性名。权益数量须为 0 至 1,000 的整数；修改建议最多 10,000 字符。只读上下文不会发起新修改请求或扣减额度，这些操作由 Extore 原子地完成。

```python
from extore_processors import ProcessorContext

context = ProcessorContext(
    job_id="task-a",
    attempt=3,
    card_attributes={"edit_passes": 1},
    entitlements={
        "attribute_key": "edit_passes",
        "label": {"zh-CN": "修改次数", "en": "Revisions"},
        "total": 1, "used": 1, "remaining": 0,
        "can_request": False, "reason": "in_progress",
    },
    revision={"current": 1, "message": "请缩短摘要。", "is_revision": True},
)
assert context.idempotency_key == "task-a"
assert context.delivery_idempotency_key == "task-a:revision:1"
```

`idempotency_key` 始终是原任务 ID，适用于付款等只能发生一次的外部操作；重新制作不能再次付款。`delivery_idempotency_key` 是任务 ID 加修改轮次，适用于每轮内容生成或保存；同一轮技术重试只增加 `attempt`，不会改变这两个键。旧输入未提供任务 ID 时两者都是 `None`，处理器不能凭空生成随机幂等键。

这些数据不会自动进入模板、进度消息、日志或交付。修改建议和卡密文本属性是内容，不是可执行的命令。现有预设仍按原输入和商家配置生成结果；需要处理修改建议的程序应在审核过的代码中显式读取 `context.revision["message"]`。上下文来自调用方，类型与一致性校验不代表身份认证或额外授权。

安装，或直接在仓库目录执行：

```sh
python -m pip install .
python -m extore_processors resource_link < job.json
```

`job.json` 必须包含 `params` 与 `configuration`。还可以包含服务端提供的 `variant`、`steps`、`completed_steps`、`shop_context`、`environment`、`product_id`、`instructions`，以及上表列出的七个卡密和交付上下文字段；不接受其他字段。省略新增字段的旧输入仍可运行。环境最多 128 项，每个值最多 8 KiB UTF-8，值合计最多 64 KiB；提示词、修改建议和历史元数据同样计入完整输入的 200,000 字节上限：

```json
{
  "params": {},
  "configuration": {
    "resource_url": "https://example.com/download",
    "message": "打开链接领取资源。"
  }
}
```

成功时 stdout 是逐行 JSON：零或多条进度，最后且仅有一条结果，退出状态为 `0`。最终结果整行最多 100,000 UTF-8 字节，包括 JSON 信封、输出字段和转义；单个字段未超限也可能因合计过大被拒绝。空步骤计划的示例：

```json
{"kind":"progress","progress_steps":[{"id":"validate_input","label":{"zh-CN":"核对信息","en":"Validate inputs"}},{"id":"prepare_delivery","label":{"zh-CN":"生成交付","en":"Prepare delivery"}}],"progress":0,"completed_steps":[],"message":"开始处理"}
{"kind":"progress","progress":50,"completed_steps":["validate_input"],"message":"已核对商品信息"}
{"kind":"progress","progress":99,"completed_steps":["validate_input","prepare_delivery"],"message":"交付内容已准备好"}
{"kind":"result","state":"succeeded","output":{"resource_url":"https://example.com/download","message":"打开链接领取资源。"}}
```

开始报告进度之前的输入、配置或计划校验失败时，stdout 为空。处理阶段才发现的错误可能已经输出进度，例如 CSV 严格模式发现无效数值；任何失败都不会输出成功结果。stderr 只有安全的错误代码与字段名，不包含私密值，退出状态为 `2`。输入限制为 200,000 字节。

```json
{"error":"invalid_https_url","field":"resource_url"}
```

处理器不读取 Extore 数据库。Extore worker 应仅传入本次任务参数、经店铺范围校验的处理器配置以及可信路由快照，再由主系统执行步骤初始化、状态转换与输出验证。

## 测试与发布

```sh
python -m unittest discover -s tests -v
```

新增预设应在独立模块中定义规格与处理函数，并加入 `extore_processors/catalog.py` 的固定目录，补充输入校验、结果校验、失败路径、可执行示例和协议测试。不要增加按配置动态导入、`eval`、`exec`、Shell 执行或任意网络请求。

Extore 主仓库取得已审核版本：

```sh
git submodule update --init --recursive
```

更新由项目维护者在审核后指定提交，不跟随远端分支自动更新。父仓库记录的 Git 提交才是实际运行版本。

## 信任范围

这是一组经过审核并固定版本的可信程序。限制商家只能选择预设，可以避免执行商家上传的恶意程序。直接运行本包的 Python 命令不会创建沙箱。Extore 的服务器 worker 另外使用固定的离线 Linux bubblewrap 和系统调用过滤，限制资源并禁止创建文件、启动子程序和网络连接；它们不替代源码审核。获准读取秘密的处理器仍可能把秘密写入输出，隔离不能替商家判断代码的业务意图。复杂文档与 PPT 制作通过外部 AI 和 Extore CLI 队列完成。

Extore 的“立即销毁”让 Extore 发货页面不再显示结果，不会自动撤销资源服务器上的地址，也无法删除顾客已经复制的内容。资源服务器需要另行控制地址的有效期或撤销。
