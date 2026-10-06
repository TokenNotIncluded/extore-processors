# Extore 官方商品处理器

这个仓库提供可以直接运行的开源商品处理器。输入什么、需要怎样填写、输出什么，都写在处理器代码里。Extore 从这些定义生成顾客表单、说明和结果校验规则。

商家选择发布版本内的预设，并填写预设声明的配置。商家不能把一个文件名、Git 地址或上传的 Python 文件当作处理器。Extore 通过 Git 子模块固定本仓库的提交，升级须审核源码、运行测试，再更新主项目的子模块版本。

MIT 许可，Python 3.11 或更新版本，只使用 Python 标准库，没有运行时依赖。

## 预设

| 预设 ID | 顾客填写 | 商家配置 | 成功输出 |
| --- | --- | --- | --- |
| `resource_link` | 无 | `resource_url` 必填；`message` 可选 | `resource_url`、`message` |
| `personalized_text` | `name` 称呼，必填，最多 200 字符 | `template` 纯文本模板 | `content` |

`resource_link` 返回商家配置的 HTTPS 资源地址和使用说明，不访问资源地址。地址可包含访问令牌，因此地址和说明均视作商家私密配置，不能通过商品展示 API 提前公开。使用说明按多行纯文本交付。

`personalized_text` 使用 `string.Template` 做文本替换，只接受 `$name`、`${name}` 和 `$$`（一个美元符号）。默认模板是：

```text
你好，$name！
你的商品已准备好。
```

模板不执行 Python、HTML、Shell 或其他代码。顾客名字里的占位符也不会再次替换。

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

`catalog()` 返回预设规格列表，`get_spec(id)` 返回某个规格。返回值是副本，调用方修改不会影响运行中的定义。`get_processor(id)` 提供带有 `.spec` 和 `.run(params, configuration)` 的只读处理器对象。

每个规格包含：

- `id`：固定预设标识；不能传文件名或模块路径。
- `schema_version`：规格版本，当前为 `1`。
- `name`、`description`：`zh-CN` 与 `en` 双语文本。
- `delivery`：当前预设均为 `content`。
- `parameters`：顾客输入字段。
- `outputs`：成功交付字段。
- `configuration`：商家配置字段；附带 `secret`、`max_length`，可选 `default`。

输入、输出和配置字段都有 `key`、双语 `label`、双语 Markdown `description`、`collapsed`、`required`、`type`。字段类型与 Extore 商品字段兼容。当前配置均标记 `secret: true`，公开规格只包含定义和通用默认模板，不能包含商家填写的配置值。

`validate_configuration(id, configuration, allow_incomplete=True)` 可以保存未填写完成的草稿，仍会检查已经填写的 URL、模板和数据类型。发行卡密和实际运行必须使用默认的完整校验。声明的默认值会应用于缺失字段；明确填写空字符串不会被默认值覆盖。

配置和顾客输入只允许代码定义的字段，值必须是字符串。资源地址必须为 HTTPS、有主机名、没有账号密码、空白或控制字符。资源地址最多 2,000 字符，模板和说明最多 10,000 字符，输出最多 100,000 字符。

`validate_parameters(id, params)` 提供与运行时相同的顾客输入校验与规范化，Extore 应在接收任务前调用它，避免必填缺失或超长的输入进入队列后才失败。

失败抛出 `ProcessorError`，带有 `.code` 和可选 `.field`，不会在错误消息中包含顾客输入或私密配置。

## 单任务命令行协议

安装，或直接在仓库目录执行：

```sh
python -m pip install .
python -m extore_processors resource_link < job.json
```

`job.json` 只能包含这两个字段：

```json
{
  "params": {},
  "configuration": {
    "resource_url": "https://example.com/download",
    "message": "打开链接领取资源。"
  }
}
```

成功时 stdout 只有一行 JSON，退出状态为 `0`：

```json
{"kind":"result","state":"succeeded","output":{"resource_url":"https://example.com/download","message":"打开链接领取资源。"}}
```

校验失败时 stdout 为空，stderr 只有安全的错误代码与字段名，退出状态为 `2`。输入限制为 200,000 字节。

```json
{"error":"invalid_https_url","field":"resource_url"}
```

处理器不读取 Extore 数据库，也不需要整店凭据。Extore worker 应仅把本次任务参数和当前商品配置传入子进程，并执行主系统的状态转换与输出验证。

## 测试与发布

```sh
python -m unittest discover -s tests -v
```

新增预设应把规格和唯一处理函数一起加入 `extore_processors/catalog.py`，补充输入校验、结果校验、失败路径和协议测试。不要增加按配置动态导入、`eval`、`exec`、Shell 执行或任意网络请求。

Extore 主仓库取得已审核版本：

```sh
git submodule update --init --recursive
```

更新由项目维护者在审核后指定提交，不跟随远端分支自动更新。父仓库记录的 Git 提交才是实际运行版本。

## 信任范围

这是一组经过审核并固定版本的可信程序。限制商家只能选择预设，可以避免执行商家上传的恶意程序。Python 子进程自身不是恶意代码沙箱；如果以后允许陌生人提交并直接运行新代码，还需要独立隔离与审核。

Extore 的“立即销毁”让 Extore 发货页面不再显示结果，不会自动撤销资源服务器上的地址，也无法删除顾客已经复制的内容。资源服务器需要另行控制地址的有效期或撤销。
