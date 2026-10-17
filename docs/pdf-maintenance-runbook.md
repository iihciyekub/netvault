# NetVault PDF 自动维护与上传查重优化运行手册

更新日期：2026-10-16。这里的“二次压缩”指 PDFCraft 优化 **PDF 文件**，不是压缩 PostgreSQL 数据库。

> **当前状态：本地已准备脚本与 cron 示例，但没有在生产服务器安装定时任务，未自动修复任何 PDF / PostgreSQL 记录。**
> 今后维护时先阅读本文件、AGENTS.md、docs/pdfcraft-postgres-maintenance.md、docs/admin-guide.md。

## 目标和执行权限

| 层级 | 操作 | 建议频率 | 是否允许自动执行 |
| --- | --- | --- | --- |
| A | 读取 PostgreSQL 中 PDFCraft 标记、Crossref 状态、空标题，生成 JSON 巡检报告 | 每 6 小时 | 是（安装示例 cron 后） |
| A+ | 读取最近 PDF 的 Document Info，标记缺少 /DOI、DOI 冲突、缺少标题/作者 | 每周一次，最多 50 份 | 是（只读） |
| B | 根据 PostgreSQL DOI 查询 Crossref/doi.org，并以 PDF 原文/Document Info 佐证标题；生成补全建议 | 按需 | 可以手动只读运行，**不建议 cron** |
| C | 给 PostgreSQL 补写**缺失**的书目信息和 Crossref 状态 | 管理员确认之后 | **必须手动 --apply、--confirm-apply，并指定最近 24 小时内有效备份清单** |
| D | PDFCraft 二次压缩、更改嵌入 DOI、删除失败 PDF | 管理员确认之后 | **不能随普通巡检自动执行** |

### 数据依据和筛选规则

- 服务 VPS：root@72.62.255.34；项目路径：/root/iiaide/netvault。
- PostgreSQL：Docker netvault-db-1 (postgres:16)；实际 PDF 在 storage/objects/<sha前缀>/<sha256>.pdf，不在数据库 BLOB。
- 标记已按当前优化配方处理的字段：pdfcraft_processed_sha256 == sha256、pdfcraft_ppi == 150、pdfcraft_quality == 75。**即使优化后没有节省空间，也可能是已处理**。
- 需要补全书目元数据：crossref_status != ok 或 SQL title 为空。报告另外统计作者、期刊名、出版年份缺失量，**但不认为每一项都必须存在**（会议、书籍、数据集可能合法缺失）。
- PDF 内嵌元数据与 PostgreSQL 书目信息分开检查。/DOI 冲突、证据不足、数字签名/加密、无法解析 PDF 必须人工审核；不得猜 DOI、不得按参考文献中的 DOI 自动判定论文身份。
- 报告默认忽略上传后不足 2 小时的记录，避免误判正在处理中的新文件。近期默认 7 天，所有符合条件的历史待办也纳入总数。
- 2026-10-16 只读现场查询时：有效 PDF 128487 条，当前配方待二次压缩 2 条，Crossref 状态非 ok 或标题缺失 706 条。这只是该时刻快照。

## 1. 每 6 小时安全巡检（默认只读）

进入 VPS 上的项目后：

    cd /root/iiaide/netvault
    .venv/bin/python scripts/audit_pdf_maintenance.py \
      --min-age-hours 2 --recent-days 7 --limit 100

默认输出：

    /root/iiaide/netvault/storage/tmp/pdf-maintenance-audit.json

结构包括：

- counts：全部符合条件的总量、最近 7 天的待压缩/待补信息量、作者/期刊/年份缺失的单独计数。
- compression_candidates：按上传时间排序，最多 100 个待压缩 PDF。
- bibliography_candidates：最多 100 个待查元数据记录。
- embedded_document_info：仅指定 --inspect-embedded N 时读取，列出内嵌 DOI 状态、与 SQL DOI 是否一致、是否存在 /Title 和 /Author。
- read_only: true：表示巡检没有进行数据库或 PDF 写入。

检查最近 50 份的 PDF Document Info（**不读取全文，不修改 PDF**）：

    .venv/bin/python scripts/audit_pdf_maintenance.py --inspect-embedded 50

本脚本不连接公开 PostgreSQL 端口，不打印数据库密码；以当前容器的内部网络地址访问数据库，只发出 SELECT 和 SET TRANSACTION READ ONLY。报告 JSON 限制为只有文件所有者可读。使用 --output 可指定输出路径。

可选的、**还没有安装**的定时计划文件：

    deploy/netvault-pdf-maintenance.example.cron

若日后经审核希望启用，先确保 VPS 代码、虚拟环境、Docker 容器、日志轮转和磁盘空间正常，再备份现有 cron 配置，将示例复制到 /etc/cron.d/ 中独立文件并检验权限/语法。示例使用 flock 防止两次巡检互相覆盖结果。不要覆盖原有 deploy/netvault-maintenance.cron；那个文件包含 PostgreSQL 备份与存储一致性检查。监测日志并确认无重复执行。**本次没有在 VPS 操作 cron。**

## 2. 书目元数据建议与有条件补写

本地脚本：scripts/repair_pdf_bibliography.py。默认是预览，不执行 UPDATE。

    cd /root/iiaide/netvault
    .venv/bin/python scripts/repair_pdf_bibliography.py --limit 20

输出：

    storage/tmp/pdf-metadata-proposals.json

程序对最多 20 条异常记录依次：

1. 确认对象存储路径合法、文件大小与 SQL 一致、SHA-256 与 SQL 一致。
2. 按 SQL 记录的 DOI 查询 Crossref/doi.org，要求返回相同规范 DOI 和有效论文标题；不把 Semantic Scholar 作为自动修复依据。
3. 使用 PDF 第一页正文标题与 DOI、或唯一内嵌 DOI + 匹配的文档标题/既有 SQL 标题进行交叉确认。
4. 对 identity 冲突、PDF 签名不明、标题证据不足或 Crossref 不一致的记录标记 review，**不进入写入候选**。
5. 对验证通过的记录只提出填补空字段的变更：title / authors / container_title / publisher / published_year / crossref_url；不覆盖现有的非空字段。
6. 若显式写入，事务中锁住目标 PDF 记录并重新核对 DOI/SHA/尺寸/路径/已有书目信息；使用项目已有 ORM 的 journal identity 事件，避免直接 UPDATE 破坏期刊名称归一化。

对 Crossref 状态已是 ok 但其它书目字段不完整的记录，可在**人工抽查**时加上：

    .venv/bin/python scripts/repair_pdf_bibliography.py --include-partial --limit 20

真正写入前，管理员先完成服务器项目与 PDF 数据的完整备份、验证日志与 manifest 文件；例如：

    NETVAULT_REPO_DIR=/root/iiaide/netvault ./scripts/backup-server.sh

然后查看新备份目录里 netvault.pg.dump、storage/objects 和 manifest.txt 均存在，确认 manifest SHA-256 与 SQL dump 一致。现有备份脚本在上传仍进行时执行，SQL dump 与 PDF 快照可能存在时间差；若用于整库/存储一致恢复或高风险 PDF 操作，应先暂停写入并做最终一致备份。收到明确执行指令后，**只进行小批量操作**：

    .venv/bin/python scripts/repair_pdf_bibliography.py \
      --limit 10 --apply --confirm-apply \
      --backup-manifest /root/netvault-data-backups/实际新备份目录/manifest.txt

脚本要求备份时间距当前不超过 24 小时，且其 SQL dump 哈希匹配；写入过程中不替换 PDF、不改 DOI、不改 SHA、不改存储路径。即使只补书目元数据，也应记录操作报告并对比样本。不要将这条 --apply 命令放进 cron。

**不足与人工检查**：扫描 PDF 可能无法取得正文；Crossref 可能不存在、限流或返回与 PDF 不符的 DOI/标题；扫描件、会议/书籍特殊结构、已签名 PDF 要人工判断。只有符合自动核实规则的记录才会进入补全候选，预览并不保证有可写入结果。

## 3. PDF 二次压缩和内嵌 DOI

已存在的业务脚本：

- scripts/compress_pdfs_parallel.py：多线程 PDFCraft 优化，可用 --pilot --limit 2 --jobs 1 小批量试运行（**真正修改生产 PDF/SQL，不是 dry-run**）。
- scripts/compress_pdfs_unprocessed.py：旧版单线程，可按需要选择但不得并行运行。
- scripts/retry_pdfcraft_failures.py：失败/特殊 DOI 修复，风险更高。
- storage/tmp/pdfcraft-batch.lock：全局协调锁；状态文件与失败日志位于 storage/tmp/ 之下。

**默认不自动运行上述任一写操作。** 要先检查备份、现有运行任务、锁、剩余空间、PDF 结构完整性，再由管理员单独授权执行。禁止通过直接写 storage/objects 中的原文件来添加 PDF DOI：这会改变 SHA-256，却未同步 PostgreSQL，导致对象丢失或下载异常。已有压缩脚本可在**候选文件更小、完整性验证通过**时协同更新 SQL；不满足条件时，不强制写入 DOI。

## 4. nv upload 为什么可能慢、优化了什么

原命令当前的主要阶段：

1. 找到目录下的所有 PDF；逐个检查头、计算或复用缓存 SHA-256，并写入本地 .netvault-upload.sqlite3。
2. **整批** SHA-256 向服务器 /pdfs/exists 批量查重（基于唯一索引）。旧实现仍会对其中 SHA 已存在的文件调用一次 PDF Document Info 解析。
3. SHA 不存在时，再读取 Document Info /DOI；如没有，可执行 pdftotext 解析最多 3 页、重新创建 pypdf Reader，扫描页面、注释、文件头和文件名推测 DOI。
4. 旧实现对新 SHA **不先通过 DOI 查重就上传整个 PDF**；服务器随后可能继续 Crossref、PDF 标题与内容校验、建立别名或判冲突。因此新哈希但旧 DOI 的文件被重复传输和分析。
5. --write-doi 额外执行远程独立 PDF 验证、原文件备份和写回，属于刻意的安全流程，不是普通快速查重。

当前优化：

- **精确 SHA 已存在**：在读取 Document Info 之前跳过，减少对大型 PDF 的无谓解析；用户明确提供的 --doi / 手工确认 DOI 冲突仍会报错。
- **不同 SHA、唯一可信内嵌 DOI 或用户手工确认 DOI 已存在**：普通上传每批准备最多 500 个 DOI，向 /pdfs/exists 批量查重并跳过已有 PDF 的文件传输（旧版每批只有 12 个）。此时不写入文件别名，也不覆盖服务器现有 PDF。
- **下载索引 / 正文提取的 DOI**：有效 SHA 绑定下载索引也纳入 DOI 集合预查，但只有第一页在参考文献之前仅发现这一 DOI、且服务器现存论文的完整标题在第一页开头得到明确印证时，才允许跳过传输。仅有索引匹配而没有 PDF 正文证明，不能当作同一论文；旧式自动发现的 CSV 记录、引用中的 DOI、多个 DOI、标题不匹配或 PDF 不可读时，都继续服务端核实。--force、--write-doi 和显式 --csv 保留原工作流；--no-skip-existing-doi 可禁用新快路。
- **进度计数含义**：`Checking / uploading PDFs 469/702 | new 1 | already stored N` 指已处理的候选数，不是已上传文件数；`new` 才是新增记录。查看完成后的报告区分 existing、uploaded、failed。
- **读操作可靠性**：SHA 和 DOI 的 `/pdfs/exists` 为只读操作，遇 429/502/503/504 或连接超时有限重试；若查重始终失败，则报错保留状态，不把网关故障当作“数据库没有 DOI”。详细阶段耗时和 SHA/DOI/服务端去重来源通过 `--verbose` 展示。
- 如需要观察实际耗时，使用 nv upload PATH --verbose，看 DOI preparation、DOI precheck、发送、响应等待及 Server-Timing；网络及 Crossref 的性能需在实机判断，不能仅凭代码估算固定秒数。
- 只靠 DOI 不等价于字节相同：同 DOI 可能有新版、更正版、不同 PDF 排版或错误元数据。如果想让服务器保留不同 PDF 的上传历史，使用 --no-skip-existing-doi；若想有意替换本体，使用受保护的 --force。

本次只改**本地源代码和文档**，不执行生产上传，不部署客户端/服务器，不启用 cron。不修改任何已有未提交业务代码之外的逻辑，也不丢弃工作树中的用户改动。未来部署前检查 Git diff、运行测试、按发布约定发版。

## 5. 验证和回滚

本地验证：

    .venv/bin/python -m pytest -q tests/test_pdf_maintenance.py tests/test_cli_helpers.py
    .venv/bin/python -m pytest -q
    .venv/bin/python -m ruff check scripts/audit_pdf_maintenance.py scripts/repair_pdf_bibliography.py src/netvault/cli/user.py tests/test_pdf_maintenance.py tests/test_cli_helpers.py

巡检任务失败只影响报告，排查 Docker 网络、psycopg、账号只读权限、schema 版本和磁盘。修复任务失败检查 JSON proposals 内的 decision/reason；不要跳过 DOI/标题核实，不要直接修改 PDF 对象。需要回退 PostgreSQL 时按照 docs/admin-guide.md / docs/migration.md 的备份恢复流程，由管理员在维护窗口内操作；切勿直接复制在线 PostgreSQL 数据卷。

实际执行注意：VPS 和本地源码文件哈希可能不同，上线前核对脚本版本；不要误把本地生成的示例 cron 当成已经生效的生产定时计划。
