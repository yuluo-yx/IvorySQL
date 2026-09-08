# FCOPY 复制完整性修复

`UTL_FILE.FCOPY` 按起止行号复制文件，首尾行均包含在范围内。省略边界时复制整个文件；最后一行没有换行符时仍需保留内容。文件内容不应因包含 NUL 字节或跨越内部缓冲区边界而丢失。

## 行为变化

旧实现使用 `fgets` 后再调用 `strlen`，NUL 后面的字节会被丢弃，换行识别也会出错。修复使用有长度的缓冲区及 `memchr` 寻找换行，保留每个选中片段的完整内容。内部缓冲区大小不会限制行长，也不会改变对外的行号含义。

旧实现以截断模式打开目标。源和目标为同一文件时，源内容会在读取前清空。修复先以不截断的模式打开目标，再比较两个已打开文件的身份；同路径、硬链接或符号链接指向同一文件时，抛出 `INVALID_OPERATION`，源内容保持不变。POSIX 使用设备号及 inode；Windows 使用打开句柄的卷标识和文件索引。

FCOPY 的内部句柄改为 PostgreSQL 的 `AllocateFile` / `FreeFile` 管理，复制失败、子事务回滚或取消操作会释放它们。逐个缓冲区检查中断，包括跳过超长行时。读取错误报告 `READ_ERROR`，写入及关闭目标时的延迟写错误报告 `WRITE_ERROR`，不再静默返回成功。

目标文件仍会被覆盖。发生 I/O 错误或取消后，目标可能为空或包含已复制的前缀；本修复不提供原子替换或文件系统事务。数据库事务回滚不会恢复文件内容。源文件并发变化时也不提供快照语义。

## 升级与回滚

无需数据迁移，直接替换扩展动态库并重启数据库进程，确保所有会话加载新实现。SQL 签名、目录登记方式和文件类型不变；`FOPEN` 的会话句柄管理不在本修复范围内。

应用应处理自复制的新错误，而不是依赖旧版清空文件的行为。原来已经丢失的文件内容只能从备份恢复。回滚可恢复旧动态库并重启，但会重新引入数据丢失和资源泄漏问题。

## 本地验证

标准回归使用独立测试数据库，验证 7 类内容、175 个行范围组合，以及默认参数、NULL 参数、空文件、同文件、目录别名、无效参数和错误恢复：

```sh
make -C contrib/ivorysql_ora oracle-installcheck ORA_REGRESS='utl_file utl_file_copy'
```

文件系统测试另外覆盖硬链接、符号链接、同一后端连续 600 次子事务错误、打开失败、读取错误和跳过 1 GiB 稀疏长行时的十次取消。测试仅依赖 Python 标准库和 IvorySQL `psql`。必须使用专用测试数据库，并指定数据库进程可见、可写的临时目录；不要指向生产环境。

```sh
PGHOST=/path/to/test/socket PGPORT=1521 PGDATABASE=postgres \
python3 contrib/ivorysql_ora/test_fcopy.py \
  --psql /path/to/ivorysql/bin/psql --workdir /path/to/test/artifacts
```

`PGPORT` 对 Python 测试应为 Oracle 端口；对 `oracle-installcheck` 应为 PostgreSQL 管理端口，回归驱动会自行连接 Oracle 端口。符号链接不可用时，脚本明确输出 `SKIP`。Windows 不运行 POSIX 目录读取错误用例。

## 依据与边界

- [Oracle 19c UTL_FILE 文档](https://docs.oracle.com/en/database/oracle/oracle-database/19/arpls/UTL_FILE.html)：FCOPY 复制连续行范围，默认复制整个文件。
- `src/include/storage/fd.h` 与 `src/backend/storage/file/fd.c`：短期 stdio 文件应通过事务感知接口分配和释放。
- 保留原有文本模式换行转换；未新增文件系统持久化保证。Windows 句柄身份分支需要在 Windows 环境进一步验证。

最后核验日期：2026-09-08。
