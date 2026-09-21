# 盲审材料隔离

原件片段采用 Unicode 码点坐标，区间左闭右开；原文与遮盖位置的版本号一起保存。

业务数据格式位于 contracts 文件，示例数据位于 data，接口进程提供 /health 健康探针。数据样例使用虚构编号，不含个人联系方式。

## 本地开发

```sh
python3 -m unittest discover -s tests -v
python3 service.py
```

服务默认监听 8080，使用 PORT 环境变量调整端口。数据结构验证不会推断记录的业务结论，未知接口返回 404。
