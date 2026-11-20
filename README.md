# 档案保全解释与整案移交（archive_handoff）

两个职责完全独立的服务，共用 SQLite 持久化，仅通过“资格产品（qualification product）”
交换数据。全部为虚构资料，不执行真实删除，也不做现实合规判断。

```
archive_handoff/
  models.py          # 不可变领域记录（Rule/Document/Event/Hold/资格产品）
  rules_service.py   # 规则/事件/保全目录 + 资格计算 + 产品的冻结与落盘
  handoff_service.py # 整案移交仓：保管责任、整案校验、准备/确认/计划、对账
  materials.py       # 只读导入 materials/751 或任意同布局目录
  scenario.py        # 文档化 751 走查、operations 回放、200/10/500/100 规模生成
  cli.py             # 命令行
  # ---- 复售证据与跨期价差 ----
  index_service.py   # 精确分数的加法（复售）指数：加权最小二乘 + 连通分量
  deeds.py           # 契据/地籍变动 → 实物身份 → 相邻两次合格成交的复售证据
tests/               # unittest（无需 pytest）
```

## 复售证据与加法指数

在既有约定（稳定 ID、单调 `revision`、追加式历史、冻结产品）之上新增两层：

- `deeds.py` 从契据与地籍变动重建**实物身份**，输出每个可比段的相邻复售证据。
- `index_service.py` 把证据当作 `s→t` 的价差边，精确求解加法指数。

### 实物身份（`deeds.py`）

- 地籍号只是**标签**；`property_id`（实物号）与档案 `deed_id`（契据号）分别保留，
  更名后实物号不变、当前标签更新。
- **纯更名** `rename`（1→1）延续同一实物，可比段跨新号继续。
- **拆分** `split`、**合并** `merge`、**实质改建** `alteration` 终止此前可比段并新建实物；
  改建即使沿用旧号（`K-3→K-3`）也视为新实物。
- **打包契据** `bundle`、**部分产权转让** `partial_interest` 不产生单宗价格；
  打包契据只有在 `allocations` 为每宗都给出正金额时才拆出单宗价。
  这两种交易本身**不造成实物变动**。
- 仅整宗独立出售有可用总价；撤销（revoked）契据不产生证据。
- 每个实物按发生顺序排序，只把**相邻**两次合格成交配对；不跨段、不跳选长区间。
  证据给出期号 `period_s/period_t`、精确价差 `delta`、`evidence_id` 与两次成交中
  **较弱**的史料等级。
- 同一实物同一顺序出现互斥变动（如对同一地籍号同时更名与拆分）时拒绝构建，
  `conflicting_change` 的明细列出涉及的**原件**（事件/契据 ID、revision、顺序）。

### 精确加法指数（`index_service.py`）

对每条纳入证据求解

```
minimise  Σ w_e · (x_t − x_s − d_e)²      subject to  x_base = 0
```

- 权重为正有理数（`int`/`"62/5"`/`Fraction`）；可按 `evidence_id` 指定，或按等级给
  `grade_weights`（证据自身权重优先），并按等级排除证据。
- 全流程 `Fraction` 精确运算，输出**精确分数串**：`0`、`12`、`62/5`。
- 期次仅在通过纳入证据与基期连通时有值；其余分量报告为 `disconnected_periods`，
  其逐证据残差仍然有定义（残差平移不变）。换基期只是整体平移。

数例（与需求一致）：`0→1 +10`、`1→2 +10`、`0→2 +26`，等权基期 0 →

| 情形 | 指数 |
| --- | --- |
| 三宗全用 | `0, 12, 24`（残差 `2, 2, −2`） |
| 去掉第三宗 | `0, 10, 20` |
| 第三宗权重 2 | `0, 62/5, 124/5` |

### 命令

```bash
DATA=artifacts/repeat

# 导入虚构契据与地籍变动（materials/752）
python3 -m archive_handoff.cli --data $DATA deed-import --materials materials/752

# 只构建复售证据（可选 --deed-rev D12=1 重开旧版）
python3 -m archive_handoff.cli --data $DATA evidence

# 精确加法指数：换基期、逐证据/按等级加权、按等级排除
python3 -m archive_handoff.cli --data $DATA index --base 0 \
  --weight 'RS:P:K-4:D8:D10=2' --grade-weight A=3 --exclude-grade C
python3 -m archive_handoff.cli --data $DATA index --deed-rev D12=1

# 自检 + 文档化走查（旧版重开、纯更名延续、未拆价契据）
python3 -m archive_handoff.cli --data $DATA verify-deeds
python3 -m archive_handoff.cli --data $DATA repeat-sale --materials materials/752
```

`materials/752` 是随附的虚构样张：C-100 演示更名延续，C-200 演示拆分终止，
C-300 演示沿用旧号的改建终止，C-400 演示未拆价打包契据（不产生价格也不变实物），
C-500 演示订正版、旧版重开、撤销契据与部分产权转让。


## 核心规则（规则/事件侧）

- 每档案有稳定 `doc_id`、`case_id`、一个或多个规则 ID。
- 每条规则给触发事件类型与非负保存天数：`计算日 = 最新适用事件日 + 保存天数`。
- 一个档案的**所有规则都满足**才谈时间条件；计算日取各规则计算日的**最大值**。
  任一必要触发缺失 → 该档案**待定**，**不把缺失事件当作第 0 天**。
- 查询日（整数）**达到**计算日即满足时间条件（`as_of >= computed`）。
- 保全可针对档案或整个 case，区间 `[start, end)`，`end` 为空表示未释放。
  查询日任一适用保全有效即禁止移交。
- **保全不暂停保存期时钟**：冻结天数不追加到到期日。D1 的计算日始终是 30，
  不是 30+25。
- 逐档案输出：计算来源（哪条规则、哪个事件、哪次修订）、有效保全、缺触发原因。
  同一档案可有**多个阻止原因**（如 `hold` + `not_due`），明细按稳定键去重，只出现一次。
- 输入批次**整批接受或整批拒绝**：同号同文复读为幂等 no-op；同号异文、
  非法区间（`end <= start`）、未知引用（未知 doc/rule/case）一律整批拒绝。
- 资格产品按固定 `as_of_day` 冻结，记录 `source_revision` 与各输入修订；
  历史产品不可被后续目录倒改。

## 移交仓（移交侧）

- 拥有**自己的**保管责任状态，初始每案卷为 `LOCAL`。责任状态 = 追加式流水
  `custody_events` + 维护的当前态 `custody_current`；打开时按流水归约对账，
  维护态被篡改会被修复。
- case 必须**整案交接**。一次请求明确 `case_id`、**完整** `doc_ids` 集合、
  `from`、`to`、单一 `as_of_day`。
  - 集合必须**恰好等于**当前案卷成员：少一个成员、或多一个，都拒绝（`not_whole_case`）。
  - 每份成员必须在该查询日的资格产品中 `releasable=true`，否则整案拒绝
    （`ineligible_members`）。
  - 当前保管人必须等于 `from`，否则拒绝（`custody_mismatch`）。
  - 单份都合格但漏成员，同样拒绝；**不能**把规则侧“可移交=true”的行直接导出就算完成。
- 移交侧**不重算**保全日期：它只消费规则侧产出的资格产品（按内容摘要校验）。
- 成功请求在**一个本地事务**内：写交接记录（固定实际成员、资格来源、交接 ID）、
  追加保管流水、更新当前保管人。
- 同 `handoff_id` 同文复读 → 只读回冻结记录；异文 → 拒绝。
- 已移交案卷再用新 ID 声称从 `LOCAL` 交出 → `custody_mismatch` 拒绝。
- **准备/计划失效**：发布计划（`prepare_and_publish`）或准备提案（`prepare`）时冻结
  成员、保管人与逐档资格摘要。确认时若成员、保管责任或相关资格任一变化，
  旧准备整批拒绝（`stale_plan` / `stale_proposal`），不会半案生效。
- **历史固定**：历史计划/提案/交接成员不按当前目录倒改。

## 命令

```bash
DATA=artifacts/demo

# 导入 materials/751（只读输入）
python3 -m archive_handoff.cli --data $DATA import-materials --materials materials/751

# 文档化 751 全流程（生成 artifacts/ 下的 P1.json / P2.json / scenario.json）
python3 -m archive_handoff.cli --data $DATA demo

# 回放 materials/751/operations.jsonl（P1 过期被拒、P2 可用、P1 历史只读）
python3 -m archive_handoff.cli --data $DATA operations

# 发布资格产品（固定查询日，落盘 products/<id>.json）
python3 -m archive_handoff.cli --data $DATA publish --day 40 --product Q40

# 修改保全（修订号递增；旧修订/异文整批拒绝）
python3 -m archive_handoff.cli --data $DATA revise-hold \
  --hold '{"hold_id":"H1","revision":2,"case_id":"C1","start":25,"end":35}'

# 单条移交请求
python3 -m archive_handoff.cli --data $DATA submit \
  --request '{"handoff_id":"WHOLE1","case_id":"C1","doc_ids":["D1","D2"],"from":"LOCAL","to":"VAULT1","as_of_day":40}'

# 从文件批量提交（每行一条完整请求；--allow-rejects 忽略个体拒绝）
python3 -m archive_handoff.cli --data $DATA requests --file my_requests.jsonl --allow-rejects

# 自定义文件导入（rules/documents/events/holds/materials）
python3 -m archive_handoff.cli --data $DATA ingest --kind holds --file extra_holds.json

# 状态与重开核对
python3 -m archive_handoff.cli --data $DATA status
python3 -m archive_handoff.cli --data $DATA verify

# 规模数据：200 档案 / 10 规则 / 500 事件+保全 / 100 请求（生成+校验+重开）
python3 -m archive_handoff.cli --data artifacts/scale scale
```

移交请求是“公开写全”的普通 JSON，可存成文件再用 `requests --file` 实际导入。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：日期算术与边界、缺触发待定、保全半开区间与不暂停时钟、多阻止原因去重、
批次原子拒绝、产品冻结、整案校验、部分拒绝、复读/异文、保管链与重开对账、
751 文档化走查、operations 回放、200/10/500/100 规模。
