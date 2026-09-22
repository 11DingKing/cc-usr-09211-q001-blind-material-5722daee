# 盲审材料隔离

基层监督专员收到匿名材料后，调查组只需要知道"发生了什么"，而接收窗口必须
保留当事人自愿提供的回访方式。本工程把接收、去标识副本生成和调查组取阅
连成完整流程：原件只向独立保管员开放，调查组只能拿到复核签名后的去标识
副本，下载链接、错误信息和访问记录都不带出联系方式。

## 角色

| 角色 | 令牌环境变量 | 职责 |
| --- | --- | --- |
| 接收窗口 | `BLIND_TOKEN_INTAKE` | 提交/补充原件，保管当事人自愿留下的回访方式 |
| 独立保管员 | `BLIND_TOKEN_CUSTODIAN` | 唯一可读原件；维护遮盖策略、生成去标识副本、查阅访问记录 |
| 复核员 | `BLIND_TOKEN_REVIEWER` | 复核副本并签名，签名后产生下载链接 |
| 调查员 | 无需令牌 | 凭下载链接取阅去标识副本，不能反查提交者 |

三个角色令牌必须互不相同，默认令牌仅供本地开发。`BLIND_SIGN_SECRET`
为复核签名的 HMAC 密钥，生产环境必须更换。

## 流程

1. 接收窗口 `POST /intake/documents` 登记原件（可同时登记回访方式）；
   后续补充用 `POST /intake/documents/{编号}/revisions`，每次补充产生新
   revision。
2. 保管员 `PUT /custodian/documents/{编号}/policy` 设置遮盖区间，每次
   更新产生新 policy_version；区间可覆盖正文、附件文件名与附件说明。
3. 保管员 `POST /custodian/documents/{编号}/copies` 按当前
   (revision, policy_version) 生成去标识副本。
4. 复核员 `GET /reviewer/copies/{副本号}` 查看副本，
   `POST /reviewer/copies/{副本号}/sign` 签名后返回下载链接
   （`POST .../reject` 则退回）。
5. 调查员 `GET /download/{令牌}` 取阅去标识副本。
6. 原件补充或策略更新会使**尚未领取**的旧副本失效（取阅返回
   `410 {"error": "副本已失效"}`），需重新生成并复核；已领取的副本
   保持可读。

所有状态落盘保存（默认 `data/state.json`，可用 `BLIND_STATE_PATH`
调整），重启后仍可凭 `GET /custodian/copies/{副本号}` 追查副本对应的
原件版本，下载链接继续有效。

## 接口一览

| 方法 | 路径 | 角色 |
| --- | --- | --- |
| GET | `/health` | 公开 |
| POST | `/intake/documents` | 接收窗口 |
| POST | `/intake/documents/{编号}/revisions` | 接收窗口 |
| PUT/GET/DELETE | `/intake/documents/{编号}/contact` | 接收窗口 |
| GET | `/custodian/documents`、`/custodian/documents/{编号}` | 独立保管员 |
| PUT | `/custodian/documents/{编号}/policy` | 独立保管员 |
| POST | `/custodian/documents/{编号}/copies` | 独立保管员 |
| GET | `/custodian/copies/{副本号}`、`/custodian/audit` | 独立保管员 |
| GET | `/reviewer/copies/{副本号}` | 复核员 |
| POST | `/reviewer/copies/{副本号}/sign`、`/reviewer/copies/{副本号}/reject` | 复核员 |
| GET | `/download/{令牌}` | 凭链接取阅 |

调用角色接口使用请求头 `Authorization: Bearer <令牌>`。错误信息统一为
通用表述（如"材料或副本不存在""副本已失效"），不携带联系方式或原文内容；
未知接口返回 404。

## 本地开发

```sh
python3 tools/make_sample.py            # 生成材料样例 data/example.json
python3 -m unittest discover -s tests -v
python3 service.py                      # 首次启动自动载入样例
```

服务默认监听 8080，使用 PORT 环境变量调整端口。材料样例使用虚构编号，
不含个人联系方式；`data/` 下除样例外的运行状态（含回访方式）已被
.gitignore 排除，不会进入版本库。数据结构验证不会推断记录的业务结论。

## 模块

- `contracts.py` — 材料样例的数据结构（原件编号、版本号、正文、片段坐标）
- `redaction.py` — Unicode 安全的遮盖引擎（码点坐标、重叠区间合并、字符簇外扩）
- `store.py` — 持久化存储：原件、回访方式、策略、副本、访问记录
- `service.py` — HTTP 接口层：路由、角色鉴权、通用错误信息
