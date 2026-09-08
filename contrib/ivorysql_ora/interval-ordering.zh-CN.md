# 大区间比较修复与升级说明

本修复适用于 `sys.dsinterval` 和 `sys.yminterval`，即 Oracle 模式的 `INTERVAL DAY TO SECOND` 与 `INTERVAL YEAR TO MONTH`。

## 行为变化

旧实现先把区间转换成有符号 64 位微秒数再比较。转换溢出后，大正区间可能小于零，大负区间可能大于零；相差恰好 `2^64` 微秒的两个不同区间甚至会比较相等。这会影响排序、比较谓词、MIN/MAX、去重、唯一索引、GiST 排斥约束和 RANGE 窗口。

例如，以下表达式在旧实现中返回 `false`，修复后返回 `true`：

```sql
SELECT INTERVAL '106751992 00:00:00' DAY(9) TO SECOND >
       INTERVAL '0 00:00:00' DAY TO SECOND;
```

修复使用 PostgreSQL 现有的 `INT128` 工具，在乘法前扩展精度；Oracle 扩展及 `ora_btree_gist` 使用一致的比较规则。窗口边界直接在宽整数中计算，避免构造越界的临时区间。支持原生 128 位整数的编译器与软件模拟实现均使用同一接口。

区间的磁盘格式、二进制收发格式、SQL 函数签名和类型精度不变。哈希仍使用原微秒表示的低 64 位，保留旧哈希值及已有哈希分区的路由结果。

## 已有数据库升级

这是比较语义修复，需要检查已有索引和依赖排序的对象，不能只替换动态库后立即恢复业务。

1. 备份数据库并安排维护窗口，暂停受影响表的写入和查询。
2. 安装修复后的 `ivorysql_ora` 与 `ora_btree_gist`，重启所有数据库进程，确保没有会话继续使用旧动态库。
3. 重新构建这两个区间类型上的 B-tree、BRIN 和 GiST 索引，包括唯一约束及排斥约束使用的索引。使用自定义操作符类的索引也需要人工核查。哈希值没有变化，内置哈希索引不因本修复要求重建。
4. 对依赖这两个类型排序的范围分区，核查分区边界及已有数据归属；重建索引不能修复错误的分区归属。必要时从备份导出数据，按正确边界重新创建分区并导入。
5. 核查包含相关比较的 CHECK 约束、部分索引条件、表达式索引和物化查询结果；按业务含义重新验证或重建。旧错误相等关系曾导致唯一约束拒绝的写入不会被自动补回。
6. 运行业务查询和区间回归，再恢复流量。

下列只读查询列出直接使用相关类型操作符类的 B-tree、BRIN 和 GiST 索引，并生成供人工审核的重建命令。它不覆盖所有间接依赖，不自动执行命令。

```sql
SELECT DISTINCT n.nspname AS index_schema,
       c.relname AS index_name,
       am.amname AS access_method,
       format('REINDEX INDEX %I.%I;', n.nspname, c.relname) AS rebuild_sql
FROM pg_catalog.pg_index AS i
JOIN pg_catalog.pg_class AS c ON c.oid = i.indexrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_am AS am ON am.oid = c.relam
CROSS JOIN LATERAL unnest(i.indclass::oid[]) AS k(opclass)
JOIN pg_catalog.pg_opclass AS oc ON oc.oid = k.opclass
WHERE oc.opcintype IN ('sys.dsinterval'::regtype, 'sys.yminterval'::regtype)
  AND am.amname IN ('btree', 'brin', 'gist')
  AND c.relkind = 'i'
ORDER BY 1, 2;
```

回滚需要在停止业务流量后恢复修复前的二进制和数据库备份。已经按新规则建立的有序索引不能直接交给旧比较逻辑使用。回滚会重新引入旧缺陷，应优先修正升级过程中的具体问题。

## 验证范围

`ora_interval_ordering` 使用独立的精确 numeric 值验证全部比较操作符、比较支持函数、排序、MIN/MAX、唯一性、哈希值和全部四种窗口边界方向，并记录负偏移的真实错误输出。它还覆盖 B-tree、哈希和 BRIN 查询。

`ora_btree_gist` 的 `interval_ordering` 验证六种比较策略、增量插入触发的索引分裂，以及原本溢出为相等的值在排斥约束中的行为。

```sh
make -C contrib/ivorysql_ora oracle-installcheck
make -C contrib/ora_btree_gist oracle-installcheck
```

测试驱动使用 PostgreSQL 管理端口创建测试数据库，再连接 Oracle 端口执行测试；自定义端口环境应把 `PGPORT` 设置为 PostgreSQL 端口。测试使用临时数据库，不能指向生产实例。

## 依据

- [Oracle 区间表达式文档](https://docs.oracle.com/html/E26088_01/expressions009.htm)：区间首字段允许指定更高精度，不能假设所有合法区间都能表示为 64 位微秒数。
- `src/backend/utils/adt/timestamp.c`：PostgreSQL 区间比较已经使用先扩展再相乘的 `INT128` 表示。
- `src/include/common/int128.h`：提供原生与软件模拟的宽整数运算接口。

文档核验日期：2026-09-08。
