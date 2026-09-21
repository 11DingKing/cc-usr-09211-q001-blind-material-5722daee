# 盲审材料隔离

原件片段采用 Unicode 码点坐标，区间左闭右开；原文与遮盖位置的版本号一起保存。

业务数据格式位于 contracts 文件，示例数据位于 data，接口进程提供 /health 健康探针。数据样例使用虚构编号，不含个人联系方式。

## 流程

接收窗口登记材料（正文、附件、自愿留下的回访方式）→ 独立保管员维护原件版本并生成去标识副本 → 复核员对副本签名 → 调查组凭一次性链接取阅。

- 正文、附件说明、文件名都按各自区间遮盖；一次遮盖可覆盖多个重叠区间，边界向外对齐字素簇，中文与表情不会被切出半个字符。
- 回访方式单独存保险库，只有接收窗口能按提交回执取回；调查员可见的链接、错误信息、访问记录都不含联系方式。
- 副本经复核签名后才能交付；原件补充或遮盖策略更新产生新版本号，尚未领取的旧副本即刻失效，已领取的副本不受影响。
- 副本记录原件编号与版本号并存入 SQLite，重启后仍可追查每份副本对应的原件版本。

## 角色与令牌

四类角色用 Bearer 令牌区分：`intake`（接收窗口）、`custodian`（独立保管员）、`reviewer`（复核员）、`investigator`（调查组）。令牌由环境变量 `ISOLATION_TOKENS` 以 JSON 配置，复核签名密钥由 `ISOLATION_SIGNING_KEY` 配置；未配置时使用 service.py 中的开发默认值，仅限本地。

## 接口摘要

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | /health | 无 | 健康探针 |
| POST | /intake/submissions | intake | 登记材料，返回 submitter_ref |
| GET | /intake/callbacks/{ref} | intake | 取回回访方式 |
| GET | /custodian/originals/{id} | custodian | 读原件最新版本 |
| GET | /custodian/originals/{id}/revisions/{n} | custodian | 读原件指定版本 |
| POST | /custodian/originals/{id}/revisions | custodian | 原件补充或遮盖策略更新（新版本号，旧副本失效） |
| POST | /custodian/originals/{id}/copies | custodian | 生成去标识副本 |
| GET | /custodian/copies[/{cid}] | custodian | 副本与原件版本对应关系 |
| GET | /reviewer/copies[/{cid}] | reviewer | 查看待复核副本 |
| POST | /reviewer/copies/{cid}/sign | reviewer | 复核签名 |
| GET | /investigator/copies | investigator | 列出可交付副本 |
| POST | /investigator/copies/{cid}/link | investigator | 签发一次性下载链接 |
| GET | /investigator/copies/{cid}/access | investigator | 查看该副本访问记录 |
| GET | /download/{token} | investigator | 凭链接取阅副本 |

## 本地开发

```sh
python3 -m unittest discover -s tests -v
python3 service.py --seed data/example.json
```

服务默认监听 8080，使用 PORT 环境变量调整端口，DB_PATH 调整 SQLite 路径（默认 data/isolation.db）。数据结构验证不会推断记录的业务结论，未知接口返回 404。
