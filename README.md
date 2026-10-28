# gitfield

[![CI](https://github.com/CastleOnTheHill/gitfield/actions/workflows/ci.yml/badge.svg)](https://github.com/CastleOnTheHill/gitfield/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)

`gitfield` 根据一个 git 仓库最近两年的提交记录，计算每个文件实际修改量最大的作者，再通过责任田花名册把文件归入责任田，结果写入本地 SQLite。

当最高修改量作者已经不在花名册中（例如离职），工具会沿目录树递归寻找最近的已直接归属文件，把当前文件归到那个文件的责任田，并明确标记回退来源和距离。

## 特性

- **git 历史归属**：按每个文件的作者增删行数聚合，默认窗口两年，可调整。
- **责任田映射**：支持长表、宽表、两列式花名册，CSV/TSV/XLSX 均可。
- **离职作者回退**：沿目录树做多源最短路径 BFS，归入最近直属文件的田。
- **可解释结果**：保留最高作者原始姓名/邮箱、回退来源、目录距离和归属方式。
- **SQLite 持久化**：重复扫描会原子替换当前快照；历史扫描记录保留。
- **命令行查询**：按责任田、员工、文件路径查询，支持人类可读表格和 JSON。
- **无第三方运行依赖**：只要求 Python 3.9+、Git 和标准库。

## 工作原理

1. 用 `git ls-tree` 取当前 HEAD 的全部文件。
2. 流式读取 `git log --numstat`，按提交者时间过滤窗口，忽略 merge commit。
3. 每个文件的每位作者累计 `新增行数 + 删除行数`，最大者即文件的最高修改人。
   同一员工有多个 git 身份时会合并统计。
4. 最高修改人通过花名册映射到责任田，文件直接归属（`direct`）。
5. 最高修改人不在花名册时：
   - 以所有直接归属文件所在的目录为种子；
   - 在目录树上求最近种子目录；
   - 继承该目录中一个直接归属文件的田；
   - 记录 `fallback_source_path`、`fallback_distance` 和 `fallback_*` 方式。
6. 没有窗口内提交历史的文件同样会回退，但标记为 `fallback_no_history`。
7. 如果仓库中完全没有直接归属文件，无法建立种子，文件标记为未归属。

### “最近文件”的定义

- 同一个目录内优先，目录距离为 `0`；同目录多个候选时，按文件名公共前缀长度选择，再按路径字典序稳定决策。
- 跨目录时按目录树边数计算距离：父目录为 `1`，兄弟目录为 `2`，依此类推。多源 BFS 选择最短距离。
- 最短距离相同时按来源文件路径字典序决策，保证重复扫描结果稳定。
- 回退来源始终是一个**直接归属**文件，避免回退链中的随机传播。
- `fallback_distance` 表示目标文件所在目录到来源目录的目录边数。

### 归属标记

| 标记 | 含义 |
| --- | --- |
| `direct` | 最高修改人属于花名册中的责任田 |
| `fallback_unmapped_author` | 最高修改人无法映射，继承了最近文件的责任田 |
| `fallback_no_history` | 窗口内没有修改记录，继承了最近文件的责任田 |
| `unassigned_unmapped_author` | 最高修改人无法映射，且仓库没有可用的直接归属种子 |
| `unassigned_no_history` | 没有窗口内历史，且仓库没有可用的直接归属种子 |

## 安装

```bash
python3 -m pip install .
gitfield --version
```

也可以直接从源码运行：

```bash
./bin/gitfield --help
```

开发环境无需安装依赖，只需要 Python 3.9+ 和 Git。

## 快速开始

```bash
gitfield --db ./ownership.db scan \
  --repo /path/to/repository \
  --roster examples/responsibility.csv \
  --author-map examples/author-map.csv

gitfield --db ./ownership.db summary
gitfield --db ./ownership.db field PAY
gitfield --db ./ownership.db employee 1001
gitfield --db ./ownership.db file services/payment/charge.py

# 机器可读输出
gitfield --db ./ownership.db file services/payment/charge.py --json
```

`--db` 可以放在子命令前后；默认是 `./gitfield.db`，也可以使用环境变量 `GITFIELD_DB`。

### 扫描窗口

```bash
# 默认两年
gitfield scan --repo /repo --roster roster.csv

# 最近 18 个月
gitfield scan --repo /repo --roster roster.csv --since 18mo

# 固定窗口，日期本身使用 UTC
gitfield scan --repo /repo --roster roster.csv \
  --since 2025-01-01 --until 2025-12-31
```

`--since` 支持 `2y`、`18mo`、`730d`、`104w` 和 ISO 日期。`--until` 只接受绝对日期，纯日期会包含当天结束时刻。不写时区的时间按 UTC 处理。

### 更新数据库

再次执行 `scan` 会重新读取当前窗口并原子替换该仓库的归属快照。旧 `scans` 记录保留，但 `file_ownership` 始终对应最新扫描。Git 历史通过 `Popen` 流式解析，不会把完整日志一次性读入内存。

```bash
gitfield --db ./ownership.db scan \
  --repo /path/to/repository \
  --roster responsibility.csv \
  --author-map author-map.csv
```

## 责任田表格

### 推荐：长表格式

一行代表“责任田 + 责任田主 + 一名员工”，同一责任田可以有多行：

```csv
field_id,field_name,owner_id,member_id
PAY,支付平台,1001,1001
PAY,支付平台,1001,1002
SEARCH,搜索推荐,2001,2001
SEARCH,搜索推荐,2001,2002
```

- `field_id` 可以省略；省略时由 `field_name` 生成稳定 ID。
- `field_name` 和 `owner_id` 必填。
- `owner_id` 会自动加入该责任田，重复列成成员也可以。
- 每名员工只能出现在一个责任田中，冲突会直接报错。
- `field_id` 或 `field_name` 均可用于 `gitfield field` 查询。

### 宽表格式

```csv
field_name,owner_id,members
搜索推荐,2001,"2002;2003"
```

`members` 支持 `,`、`;`、`，`、`；`、`、` 和空白分隔。

### 两列式花名册

如果表格只有责任田主和员工两列，则以责任田主为单位生成责任田，田名为 `owner:<工号>`：

```csv
owner_id,member_id
1001,1001
1001,1002
```

### 格式支持

- `.csv`、`.tsv`、`.txt`，自动识别逗号/分号/Tab，文本编码支持 UTF-8 和 GB18030。
- `.xlsx`/`.xlsm` 读取第一个工作表，无需安装 `openpyxl`。
- 旧版 `.xls` 不支持，请另存为 `.xlsx` 或 CSV。
- 工号建议在 Excel 中设为文本格式；纯数字工号会按 Excel 存储值读取，前导零可能丢失。
- 表头支持中英文别名，例如 `责任田名称`、`责任田主工号`、`员工工号`、`field_name`、`owner_id`、`member_id`。

## Git 作者映射到工号

责任田表只有工号，因此需要把 Git 的 `user.name`/`user.email` 映射到工号。解析优先级从高到低：

1. `--author-map` 中的显式邮箱或姓名。
2. 花名册可选列（`员工邮箱`、`owner_emails`、`git_name` 等）中的别名。
3. 自动推断：
   - 邮箱前缀就是工号：`1001@corp.example`
   - 邮箱前缀含工号 token：`1001+ci@corp.example`、`alice.1001@corp.example`
   - Git 姓名含工号 token：`Alice (1001)`
4. 无法唯一匹配时**不猜测**，按未映射处理并进入最近文件回退。

推荐为不符合上述约定的企业邮箱提供映射：

```csv
employee_id,email,name
1002,bob.wang@corp.example,Bob Wang
2002,carol.li@corp.example,Carol Li
```

一个员工可以有多行、多个邮箱，统计时会合并。`gitfield summary` 会列出未映射的最高作者及其文件数和修改量，便于补全映射。

## 查询

### 某个责任田的所有文件

```bash
gitfield --db ownership.db field PAY
gitfield --db ownership.db field 支付平台
```

输出包含路径、最高修改人、归属方式、回退来源、目录距离和修改量。

### 某个人负责的文件

```bash
gitfield --db ownership.db employee 1001
gitfield --db ownership.db employee 1001 --direct-only
```

默认包含：

- 该员工是最高修改人的直接归属文件；
- 该员工作为责任田主承接的离职作者回退文件。

`--direct-only` 只返回前者。输出的 `ROLE` 列会区分 `top_modifier` 和 `field_owner_fallback`。责任田主若想查看整个田，应使用 `field` 查询。

### 某个文件的归属

```bash
gitfield --db ownership.db file services/payment/charge.py
gitfield --db ownership.db file /absolute/path/to/repository/services/payment/charge.py
```

显示责任田、田主、最高修改人、修改量、最近提交、归属方式、回退来源、目录距离和当前责任人。

### 汇总

```bash
gitfield --db ownership.db summary
```

显示扫描窗口、提交数、直接/回退/未归属文件数、各责任田文件数，以及未映射的最高作者。

### JSON 与多仓库

所有查询和 `scan` 都支持 `--json`：

```bash
gitfield --db ownership.db field PAY --json
```

一个数据库可以保存多个仓库。库中只有一个仓库时，查询可省略 `--repo`；有多个时需指定：

```bash
gitfield --db ownership.db summary --repo /path/to/repository
```

## SQLite 数据模型

| 表 | 用途 |
| --- | --- |
| `repos` | 仓库标识、来源、本地路径和当前 HEAD |
| `fields` | 本次扫描的责任田与田主 |
| `employees` | 工号、所属责任田、是否田主 |
| `scans` | 每次扫描的窗口、HEAD、花名册哈希和统计 |
| `file_ownership` | 每个当前文件的归属、最高作者、回退证据 |
| `file_authors` | 每个文件每位作者的新增/删除/提交证据 |

`file_ownership` 的关键列：

- `field_id`
- `top_employee_id`：最高修改人，未映射时为 `NULL`
- `top_author_name` / `top_author_email`：原始最高作者
- `top_additions` / `top_deletions`
- `assignment`
- `fallback_source_path` / `fallback_distance`
- `responsible_employee_id`：直接归属时为最高修改人；回退时为责任田主

直接查询示例：

```sql
SELECT path, assignment, top_employee_id, fallback_source_path
FROM file_ownership
WHERE field_id = 'PAY'
ORDER BY path;

SELECT path, author_name, author_email, additions, deletions
FROM file_authors
WHERE path = 'services/payment/charge.py'
ORDER BY additions + deletions DESC;
```

## 验收

```bash
make test     # 31 个单元/集成测试
make accept   # 14 项端到端验收检查
```

验收脚本会在临时目录生成一个真实的 git 仓库，包含：

- 两年内的直接归属文件；
- 四年前被前员工大量修改、但窗口内由在职员工修改的文件；
- 离职作者修改的兄弟目录文件；
- 离职作者在两层子目录中创建的文件；
- 文件重命名；
- 只在窗口外有历史的文件；
- 未被自动识别的企业邮箱；
- 重新扫描后新作者接管文件、新责任田文件加入。

验收报告见 [docs/acceptance-report.md](docs/acceptance-report.md)。

如需保留演示仓库：

```bash
python3 scripts/acceptance.py \
  --keep-dir ./demo-workdir \
  --report docs/acceptance-report.md
```

## 已知边界

- 修改量按 git 行级增删统计；二进制文件可能没有行数，只能靠提交次数打破平局。
- 默认忽略 merge commit，因此不会统计 merge 冲突解决中的手工改动。
- 重命名提交归到新路径；同一窗口内重命名之前旧路径上的历史不会追溯合并。
- 没有路径过滤（例如 vendored/生成代码也会参与统计），需要时可在仓库侧拆分或在后续版本增加 exclude。
- 重复扫描是窗口内全量重算，不是按 commit 做增量累计，但 Git 输出是流式解析的。
- 自动身份推断只接受无歧义匹配；企业邮箱差异较大时应提供 `--author-map`。
- 未映射作者的文件只有在仓库存在直接归属种子时才能回退，否则会明确标记为未归属。

## 项目结构

```text
src/gitfield/
  git_history.py   git 仓库检查、流式 numstat 解析
  roster.py        责任田/作者映射表解析
  xlsx.py          无依赖 XLSX 读取
  identity.py      git 作者到工号的解析
  ownership.py     修改量聚合、目录树回退
  db.py            SQLite schema、写入和查询
  scanner.py       扫描编排
  cli.py           命令行入口
tests/             单元与端到端测试
scripts/           验收脚本
examples/          示例表格
```

## License

MIT
