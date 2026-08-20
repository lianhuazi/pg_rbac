# pg_rbac PostgreSQL14+ 权限管理工具
### 掉了好几根头发<img width="200" height="200" alt="image" src="https://github.com/user-attachments/assets/13a2eed2-c097-445f-b22b-656ec58f885c" />
### 大佬请，大佬请留步。嘻嘻嘻，有烟么，能请我抽一口烟么。
<img width="200" height="280" alt="微信图片_20260820112559" src="https://github.com/user-attachments/assets/febd1fc0-600c-4f9d-ba80-d0f0949685dc" />  


## REPL  --dry-run 预演模式输出sql 
```bash
uv run pg_rbac_beta.py --dry-run 
```
### 主要功能模块，tab 功能补全参数
pg_rbac[-pre][DRY]> help
────────────────────────────────────────────────────────────────────────────────────── PostgreSQL RBAC 权限管理工具 ──────────────────────────────────────────────────────────────────────────────────────
```python
用法:  pg_rbac.py [选项] <模块> <子命令> [参数]

全局选项:
  -c, --config    配置文件路径 (默认 config.yaml)  
  --dry-run       只生成SQL不执行                  
  -h, --help      显示帮助信息                     

功能模块:
  role        角色管理（权限组，无登录权限）      
  user        用户管理（可登录账号）              
  priv        权限管理（授权/回收/默认权限）      
  query       权限查询（查看用户/角色权限）       
  template    一键模板（自动创建角色+用户+权限）  

使用 pg_rbac.py <模块> -h 查看模块详细帮助
```
### 使用例子
```python
template readwrite
Usage: template readwrite <user> -p pwd -d db -s schema -o owner
pg_rbac[-pre][DRY]> template readwrite test-rw -p 1213 -d xxx -s public -o root
[ERROR] 切换数据库失败: FATAL:  database "xxx" does not exist

Error: FATAL:  database "xxx" does not exist

pg_rbac[-pre][DRY]> template readwrite test-rw -p 1213 -d xxl-job -s public -o root
已切换到数据库: xxl-job
╭───────────────────────────────────────────────────────────────────────────────────────────── 读写用户模板 ─────────────────────────────────────────────────────────────────────────────────────────────╮
│  用户名: test-rw                                                                                                                                                                                       │
│  角色名: r_test-rw_readwrite                                                                                                                                                                           │
│  数据库: xxl-job                                                                                                                                                                                       │
│  Schema: public                                                                                                                                                                                        │
│  建表者: root                                                                                                                                                                                          │
│  权限: CONNECT+TEMPORARY, USAGE+CREATE(Schema), ALL PRIVILEGES(表+序列+函数+类型), USAGE(语言)                                                                                                         │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
───────────────────────────────────────────────────────────────────────────────────────────── 创建角色和用户 ─────────────────────────────────────────────────────────────────────────────────────────────
-- SQL: CREATE ROLE "r_test-rw_readwrite" NOLOGIN
-- SQL: COMMENT ON ROLE "r_test-rw_readwrite" IS '读写角色'
-- SQL: CREATE ROLE "test-rw" WITH LOGIN PASSWORD '1213'
-- SQL: COMMENT ON ROLE "test-rw" IS '读写用户'
-- SQL: GRANT "r_test-rw_readwrite" TO "test-rw"
─────────────────────────────────────────────────────────────────────────────────────────── 授权数据库和Schema ───────────────────────────────────────────────────────────────────────────────────────────
-- SQL: GRANT CONNECT,TEMPORARY ON DATABASE "xxl-job" TO "r_test-rw_readwrite"
-- SQL: GRANT USAGE,CREATE ON SCHEMA "public" TO "r_test-rw_readwrite"
────────────────────────────────────────────────────────────────────────────────────────────── 授权对象权限 ──────────────────────────────────────────────────────────────────────────────────────────────
-- SQL: GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON ALL TABLES IN SCHEMA "public" TO "r_test-rw_readwrite"
-- SQL: GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA "public" TO "r_test-rw_readwrite"
-- SQL: GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA "public" TO "r_test-rw_readwrite"
-- SQL: GRANT USAGE ON TYPE "<type_name>" TO "r_test-rw_readwrite"
-- 注：PG 不支持 GRANT ... ON ALL TYPES IN SCHEMA，实际执行时逐个类型授权
-- SQL: GRANT USAGE ON LANGUAGE "<language_name>" TO "r_test-rw_readwrite"
-- 注：PG 不支持 GRANT ... ON ALL LANGUAGES，实际执行时逐个语言授权（如 plpgsql、plpython3u 等）
────────────────────────────────────────────────────────────────────────────────────── 设置默认权限（未来新建对象） ──────────────────────────────────────────────────────────────────────────────────────
-- SQL: ALTER DEFAULT PRIVILEGES FOR ROLE "root" IN SCHEMA "public" GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON TABLES TO "r_test-rw_readwrite"
-- SQL: ALTER DEFAULT PRIVILEGES FOR ROLE "root" IN SCHEMA "public" GRANT USAGE,SELECT,UPDATE ON SEQUENCES TO "r_test-rw_readwrite"
-- SQL: ALTER DEFAULT PRIVILEGES FOR ROLE "root" IN SCHEMA "public" GRANT EXECUTE ON FUNCTIONS TO "r_test-rw_readwrite"
-- SQL: ALTER DEFAULT PRIVILEGES FOR ROLE "root" IN SCHEMA "public" GRANT USAGE ON TYPES TO "r_test-rw_readwrite"
pg_rbac[-pre][DRY]> template readwrite test-rw -p 1213 -d xxl-job -s public -o root -h
────────────────────────────────────────────────────────────────────────────────────────── template - 一键模板 ───────────────────────────────────────────────────────────────────────────────────────────

用法:  pg_rbac.py template <子命令> [参数]

  backup <用户名> -p 密码 -d 库名 -o 建表者                    创建备份账号模板 (SELECT表 + SELECT,USAGE序列)                      
  readonly <用户名> -p 密码 -d 库名 -s Schema -o 建表者        创建只读用户模板 (SELECT表+序列)                                    
  dml <用户名> -p 密码 -d 库名 -s Schema -o 建表者             创建DML应用写入模板 (SELECT,INSERT,UPDATE,DELETE表+序列+函数+类型)  
  readwrite <用户名> -p 密码 -d 库名 -s Schema -o 建表者       创建读写用户模板 (ALL PRIVILEGES+CREATE Schema+语言)                
  dba <用户名> -p 密码 -d 库名 -s Schema -o 建表者             创建DBA管理员模板 (ALL PRIVILEGES+CREATEROLE+pg_read_all_stats)     
  apply <角色名> -t 模式 -d 库名 -s Schema -o 建表者           对已有角色应用权限模板                                              
  apply-append <角色名> -t 模式 -d 库名 -s Schema -o 建表者    对已有角色追加权限                                                  
  revoke-all <角色名> -d 库名 [-s Schema]                      一键回收角色所有权限                                                

  -t 权限模式: readonly / dml / readwrite / dba / backup
  -o, --owner-role  建表者角色（用于设置默认权限）
 ```
 ## REPL 模式  tab 功能补全参数
 ```bash
  uv run pg_rbac_beta.py 
```
### 命令使用
```python
pg_rbac[-pre]> template readwrite test1-rw -p 1213 -d xxl-job -s public -o dbroot
╭───────────────────────────────────────────────────────────────────────────────────────────── 读写用户模板 ─────────────────────────────────────────────────────────────────────────────────────────────╮
│  用户名: test1-rw                                                                                                                                                                                      │
│  角色名: r_test1-rw_readwrite                                                                                                                                                                          │
│  数据库: xxl-job                                                                                                                                                                                       │
│  Schema: public                                                                                                                                                                                        │
│  建表者: dbroot                                                                                                                                                                                        │
│  权限: CONNECT+TEMPORARY, USAGE+CREATE(Schema), ALL PRIVILEGES(表+序列+函数+类型), USAGE(语言)                                                                                                         │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
───────────────────────────────────────────────────────────────────────────────────────────── 创建角色和用户 ─────────────────────────────────────────────────────────────────────────────────────────────
[OK] 角色 r_test1-rw_readwrite 创建成功
[OK] 用户 test1-rw 创建成功
[OK] 用户 test1-rw 已继承角色 r_test1-rw_readwrite
─────────────────────────────────────────────────────────────────────────────────────────── 授权数据库和Schema ───────────────────────────────────────────────────────────────────────────────────────────
[OK] 角色 r_test1-rw_readwrite 获得数据库 xxl-job 的 CONNECT,TEMPORARY 权限
[OK] 角色 r_test1-rw_readwrite 获得 Schema public 的 USAGE,CREATE 权限
────────────────────────────────────────────────────────────────────────────────────────────── 授权对象权限 ──────────────────────────────────────────────────────────────────────────────────────────────
[OK] 角色 r_test1-rw_readwrite 已获得 Schema public 下所有表的 SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER 权限
[OK] 角色 r_test1-rw_readwrite 已获得 Schema public 下所有序列的 USAGE,SELECT,UPDATE 权限
[OK] 角色 r_test1-rw_readwrite 已获得 Schema public 下所有函数的 EXECUTE 权限
[OK] 角色 r_test1-rw_readwrite 已获得 Schema public 下 8 个类型的 USAGE 权限
[OK] 角色 r_test1-rw_readwrite 已获得 1 种过程语言的 USAGE 权限
────────────────────────────────────────────────────────────────────────────────────── 设置默认权限（未来新建对象） ──────────────────────────────────────────────────────────────────────────────────────
[OK] 已设置 Schema public 下新建TABLES的默认权限
[OK] 已设置 Schema public 下新建SEQUENCES的默认权限
[OK] 已设置 Schema public 下新建FUNCTIONS的默认权限
[OK] 已设置 Schema public 下新建TYPES的默认权限
╭────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│  读写用户创建完成！                                                                                                                                                                                    │
│                                                                                                                                                                                                        │
│  用户 test1-rw 拥有全部表权限(含TRUNCATE/TRIGGER)和Schema创建权限                                                                                                                                      │
│                                                                                                                                                                                                        │
│  注意:                                                                                                                                                                                                 │
│    1. 默认权限只对 dbroot 创建的新对象生效                                                                                                                                                             │
│       若有其他角色也会建表，需额外执行 set-default 指定其他建表者                                                                                                                                      │
│    2. 已存在的老表已通过 GRANT ALL TABLES 授权                                                                                                                                                         │
│       未来新建的表走默认权限自动授权                                                                                                                                                                   │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
pg_rbac[-pre]> 
```

## CLI 命令行模式,没有命令行参数提升功能 部分使用实例
```bash
[root@jenkins py]# uv run pg_rbac_beta.py -h
────────────────────────────────────────────────────────────────────────────────────── PostgreSQL RBAC 权限管理工具 ──────────────────────────────────────────────────────────────────────────────────────

用法:  pg_rbac.py [选项] <模块> <子命令> [参数]

全局选项:
  -c, --config    配置文件路径 (默认 config.yaml)  
  --dry-run       只生成SQL不执行                  
  -h, --help      显示帮助信息                     

功能模块:
  role        角色管理（权限组，无登录权限）      
  user        用户管理（可登录账号）              
  priv        权限管理（授权/回收/默认权限）      
  query       权限查询（查看用户/角色权限）       
  template    一键模板（自动创建角色+用户+权限）  

使用 pg_rbac.py <模块> -h 查看模块详细帮助
```
###  用户模块功能解释
```bash
[root@jenkins py]# uv run pg_rbac_beta.py user list
────────────────────────────────────────────────────────────────────────────────────── PostgreSQL RBAC 权限管理工具 ──────────────────────────────────────────────────────────────────────────────────────
                                                      用户列表                                                      
┏━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 用户名     ┃ 状态 ┃ 过期时间                         ┃ 超级用户 ┃ 继承权限 ┃ 所属角色                             ┃
┡━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ dbroot    │ 启用 │ 永不过期                         │ 是       │ 是       │ 无                                   │
│ pre_r     │ 启用 │ 9999-12-31 23:59:59.999999+00:00 │ 否       │ 是       │ pre_role_r                           │
│ pre_rw    │ 启用 │ 永不过期                         │ 否       │ 是       │ pre_role_rw                          │
│ ta-dba    │ 启用 │ 永不过期                         │ 否       │ 是       │ "r_ta-dba_dba", pg_read_all_stats │
│ test-rw   │ 启用 │ 永不过期                         │ 否       │ 是       │ "r_test-rw_readwrite"                │
│ test1-rw  │ 启用 │ 永不过期                         │ 否       │ 是       │ "r_test1-rw_readwrite"               │
└───────────┴──────┴──────────────────────────────────┴──────────┴──────────┴──────────────────────────────────────┘
[root@jenkins py]# uv run pg_rbac_beta.py user -h
──────────────────────────────────────────────────────────────────────────────────────────── user - 用户管理 ─────────────────────────────────────────────────────────────────────────────────────────────

用法:  pg_rbac.py user <子命令> [参数]

  create <用户名> -p 密码 [-m 备注]    创建可登录用户            
  drop <用户名>                        删除用户                  
  passwd <用户名> -p 新密码            修改用户密码              
  disable <用户名>                     禁用用户（禁止登录）      
  enable <用户名>                      启用用户（允许登录）      
  grant-role <用户名> <角色名>         给用户授予角色            
  revoke-role <用户名> <角色名>        回收用户的角色            
  list                                 列出所有可登录用户及角色  
  
  
```
### 权限授权功能
```bash
[root@jenkins py]# uv run pg_rbac_beta.py priv -h
──────────────────────────────────────────────────────────────────────────────────────────── priv - 权限管理 ─────────────────────────────────────────────────────────────────────────────────────────────

用法:  pg_rbac.py priv <子命令> [参数]

通用选项: -d, --dbname <数据库名>  切换到指定数据库执行

━━ 授权命令 (grant-*) ━━
  grant-db <角色> <库名> [-p 权限]                      数据库级授权 (权限: CONNECT,CREATE,TEMPORARY)                                       
  grant-schema <角色> <Schema> [-p 权限]                Schema级授权 (权限: USAGE,CREATE)                                                   
  grant-table <角色> <Schema> <表> [-p 权限]            单表授权 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)            
  grant-table-regex <角色> <Schema> <正则> [-p 权限]    按正则批量授权表 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)    
  grant-all-tables <角色> <Schema> [-p 权限]            授权Schema下所有表 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)  
  grant-all-sequences <角色> <Schema> [-p 权限]         授权Schema下所有序列 (权限: USAGE,SELECT,UPDATE)                                    
  grant-all-functions <角色> <Schema> [-p 权限]         授权Schema下所有函数 (权限: EXECUTE)                                                
  grant-all-types <角色> <Schema> [-p 权限]             授权Schema下所有类型 (权限: USAGE)                                                  
  grant-all-languages <角色> [-p 权限]                  授权所有过程语言 (权限: USAGE)                                                      

━━ 默认权限 (set-default / revoke-default) ━━
  set-default <角色> <Schema>  [-T 类型] [-p 权限] [-o 建表者]     设置新建对象的默认权限  
  revoke-default <角色> <Schema>  [-T 类型] -p 权限 [-o 建表者]    回收新建对象的默认权限  
  -T TABLES      -p 可选: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER (默认SELECT)
  -T SEQUENCES   -p 可选: USAGE,SELECT,UPDATE (默认SELECT)
  -T FUNCTIONS   -p 可选: EXECUTE (默认EXECUTE)
  -T TYPES       -p 可选: USAGE (默认USAGE)

━━ 回收命令 (revoke-*) ━━
  revoke-db <角色> <库名> -p 权限                      回收数据库权限 (权限: CONNECT,CREATE,TEMPORARY)                                         
  revoke-schema <角色> <Schema> -p 权限                回收Schema权限 (权限: USAGE,CREATE)                                                     
  revoke-table <角色> <Schema> <表> -p 权限            单表回收权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)            
  revoke-table-regex <角色> <Schema> <正则> -p 权限    按正则批量回收表权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)    
  revoke-all-tables <角色> <Schema> -p 权限            回收Schema下所有表权限 (权限: SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER)  
  revoke-all-sequences <角色> <Schema> -p 权限         回收Schema下所有序列权限 (权限: USAGE,SELECT,UPDATE)                                    
  revoke-all-functions <角色> <Schema> -p 权限         回收Schema下所有函数权限 (权限: EXECUTE)                                                
  revoke-all-types <角色> <Schema> -p 权限             回收Schema下所有类型权限 (权限: USAGE)                                                  
  revoke-all-languages <角色> -p 权限                  回收所有过程语言权限 (权限: USAGE)          
  ```
 ### 查询模块
 ```bash
 
用法:  pg_rbac.py query <子命令> [参数]

通用选项: -d, --dbname <数据库名>  切换到指定数据库查询

  user <用户名> [-f 表筛选] [-v]    查询用户完整权限（级联所有继承角色）  
  role <角色名> [-f 表筛选] [-v]    查询角色完整权限（级联所有继承角色）  

  -f, --table-filter  表名筛选 (SQL LIKE, 如 'ac_%')
  -v, --verbose        显示全部权限（含序列/函数/类型/语言）
  ```
  #### 使用实例
  ```bash
  [root@jenkins py]# uv run pg_rbac_beta.py query role pre_role_r -d xxl-job
────────────────────────────────────────────────────────────────────────────────────── PostgreSQL RBAC 权限管理工具 ──────────────────────────────────────────────────────────────────────────────────────
已切换到数据库: xxl-job
╭─────────────────────────────────────────────────────────────────────────────────────────────── 主体信息 ───────────────────────────────────────────────────────────────────────────────────────────────╮
│  主体:    pre_role_r                                                                                                                                                                                   │
│  类型:    角色(权限组)                                                                                                                                                                                 │
│  超级用户: 否                                                                                                                                                                                          │
│  继承权限: 是                                                                                                                                                                                          │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
 有效角色链（包含主体自身） 
┏━━━━━━━━━━━━┳━━━━━━┳━━━━━━┓
┃ 角色/用户  ┃ 类型 ┃ 标记 ┃
┡━━━━━━━━━━━━╇━━━━━━╇━━━━━━┩
│ pre_role_r │ 角色 │      │
└────────────┴──────┴──────┘

未继承其他角色
       数据库级显式 ACL 权限        
┏━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━┓
┃ 数据库    ┃ 权限    ┃ 授权给     ┃
┡━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━┩
│ ta-pre │ CONNECT │ pre_role_r │
│ xxl-job   │ CONNECT │ pre_role_r │
└───────────┴─────────┴────────────┘
    Schema 级显式 ACL 权限     
┏━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┓
┃ Schema ┃ 权限  ┃ 授权给     ┃
┡━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━┩
│ public │ USAGE │ pre_role_r │
└────────┴───────┴────────────┘
                   表/视图级显式 ACL 权限                   
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
┃ Schema ┃ 对象名             ┃ 类型 ┃ 权限   ┃ 授权给     ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
│ public │ xxl_job_group      │ T    │ SELECT │ pre_role_r │
│ public │ xxl_job_info       │ T    │ SELECT │ pre_role_r │
│ public │ xxl_job_lock       │ T    │ SELECT │ pre_role_r │
│ public │ xxl_job_log        │ T    │ SELECT │ pre_role_r │
│ public │ xxl_job_log_report │ T    │ SELECT │ pre_role_r │
│ public │ xxl_job_logglue    │ T    │ SELECT │ pre_role_r │
│ public │ xxl_job_registry   │ T    │ SELECT │ pre_role_r │
│ public │ xxl_job_user       │ T    │ SELECT │ pre_role_r │
└────────┴────────────────────┴──────┴────────┴────────────┘
╭─────────────────────────────────────────────────────────────────────────────────────────────── 查询说明 ───────────────────────────────────────────────────────────────────────────────────────────────╮
│  1. 显式 ACL 权限表示 PostgreSQL ACL 中实际存在的 GRANT（包括 PUBLIC ACL）。                                                                                                                           │
│  2. 主体直接 GRANT 的权限也会统计，不再只看继承角色。                                                                                                                                                  │
│  3. 角色继承会递归展开，例如 user -> role_a -> role_b。                                                                                                                                                │
│  4. SUPERUSER 的权限属于系统隐含权限，不依赖 ACL。                                                                                                                                                     │
│  5. 类型列: T=TABLE(普通表), V=VIEW(视图), M=MATERIALIZED VIEW(物化视图), P=PARTITIONED TABLE(分区表), F=FOREIGN TABLE(外部表)                                                                         │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```
都看到这儿来了。打赏一口呗
<img width="200" height="280" alt="微信图片_20260820112952" src="https://github.com/user-attachments/assets/05d54d0e-8b32-48cf-99a2-853affe013fa" />
  
 
