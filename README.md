# 数仓分层建模与治理平台

面向原始数据落地、清洗转换、分层建模、维度事实设计、分区治理、质量校验、血缘追踪与历史补数的数据仓库工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 原始落地 | `landing` | 落地批次 | 批次编号、来源系统、落地路径 |
| 贴源层 | `ods` | 贴源表 | 表编号、表名称、来源系统 |
| 明细层 | `dwd` | 明细表 | 表编号、表名称、业务域 |
| 维度层 | `dim` | 维度表 | 维度编号、维度名称、自然键列 |
| 汇总层 | `dws` | 汇总表 | 表编号、表名称、统计粒度 |
| 应用层 | `ads` | 应用表 | 表编号、表名称、业务场景 |
| 分区治理 | `partition` | 分区方案 | 方案编号、数据表名、分区字段 |
| 质量校验 | `quality` | 质量规则 | 规则编号、校验表名、目标字段 |
| 数据血缘 | `lineage` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 历史补数 | `backfill` | 补数任务 | 任务编号、目标表名、开始日期 |
| 清洗规则 | `cleanrule` | 清洗规则 | 规则编号、输入表名、空值策略 |
| 转换编排 | `transform` | 转换节点 | 节点编号、输入表名、输出表名 |
| 数据字典 | `dictionary` | 字典条目 | 条目编号、表名、字段名称 |
| 元数据管理 | `metamodel` | 元数据项 | 元数据编号、对象类型、对象名称 |
| 数据生命周期 | `lifecycle` | 生命周期策略 | 策略编号、数据表名、保留天数 |
| 存储成本 | `cost` | 成本账单 | 账单编号、数据表名、存储大小GB |
| 同步通道 | `sync` | 同步通道 | 通道编号、来源表名、目标表名 |
| 变更审计 | `auditlog` | 变更记录 | 记录编号、操作表名、变更类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message, codes }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 入湖链路：登记快照 → 校验准入 → 归档

原始落地（landing）与贴源层（ods）通过共享校验快照
（`backend/app/services/snapshot.py`，内存表 `snapshot`）串联，两个入口对
"哪个版本可用"给出同一份判断。

### 状态与门禁

- 落地批次只能按 `草稿 → 落地中 → 已落地 → 已归档` 推进；草稿跨到归档、
  落地中直接归档、归档后重开文件/重复导入同编号文件都会被拒
  （`ILLEGAL_TRANSITION` / `ARCHIVED_BATCH_LOCKED`）。
- 登记时完整保存业务字段：文件格式、接收时刻、原始行数、摘要算法、摘要值、
  文件大小、`source_manifest`。原始登记一旦提交即持久化，后续任何回滚都不删除它。
- "确认已落地"提交校验快照，必须同时满足：文件大小 > 0、压缩包内只有一个文件、
  摘要算法与摘要值齐全、实测行数与登记行数/清单行数一致。失败只落 `FAILED`
  快照作为恢复锚点，不推进状态、不写审计。可区分的失败码包括：
  `EMPTY_FILE`、`MULTI_FILE_ARCHIVE`、`DIGEST_MISSING`、`ROW_COUNT_MISMATCH`、
  `SHARD_CONFLICT`（同摘要不同大小的分片冲突）、`DUPLICATE_FILE`（同名重传）。
- 同批次并发确认只有首份完整快照能提交，后到者收到 `SNAPSHOT_ALREADY_COMMITTED`。
- 快照记录 `snapshot_id`、`digest_type`、`digest_value`、`byte_size`、
  `completed_at`、`source_manifest`；失败后凭 `snapshot_id` 续传会原地转正，
  不会新建重复快照。
- 贴源只准入/读取 `PASSED` 快照的批次：`POST /api/ods/{id}/admit` 与
  `GET /api/ods/readable-batches`。快照、落地批次、贴源关系、审计在同一事务内
  提交，异常按模块整体回滚，不留半套审计。

### 验收测试

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

覆盖字段完整持久化、空文件/多文件/重传的可区分结论、同摘要不同大小判分片冲突、
已归档批次同编号再导入被拒、并发确认只接受一份、贴源只读取校验通过批次、
事务回滚不删除原始登记、按 `snapshot_id` 恢复等 29 个用例。

领域：数据工程
子方向：数仓与数据湖
