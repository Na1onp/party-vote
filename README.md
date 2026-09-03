# 聚会投票后端

Flask + SQLite 实现的聚会投票系统后端。

## 本地运行

```bash
# 1. 进入项目目录
cd 聚会投票-backend

# 2. 创建虚拟环境（如果还没创建）
python3 -m venv venv
source venv/bin/activate  # Mac/Linux
# venv\Scripts\activate  # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动服务
python app.py
```

服务默认运行在 `http://127.0.0.1:8080`

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/users | 创建用户 |
| GET | /api/users/:id | 获取用户 |
| POST | /api/projects | 创建项目 |
| POST | /api/projects/join | 加入项目 |
| GET | /api/projects/:code | 获取项目详情 |
| POST | /api/projects/:code/complete | 标记项目完成 |
| POST | /api/projects/:code/restore | 恢复项目 |
| DELETE | /api/projects/:code | 删除项目 |
| POST | /api/votes | 提交/更新投票 |
| GET | /api/votes/:project_id | 获取项目投票 |
| DELETE | /api/votes/:project_id/:user_id | 删除投票 |
| GET | /api/health | 健康检查 |

## 部署到 Render（免费）

1. 把整个 `聚会投票-backend` 文件夹上传到 GitHub
2. 注册 [Render](https://render.com)
3. 创建 New Web Service，选择 GitHub 仓库
4. 配置：
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
5. 点击部署，获得一个 `https://xxx.onrender.com` 的链接
6. 前端把 API 地址改成这个链接即可

## 前端连接

前端需要在 HTML 里把 `localStorage` 读写改为调用这些 API。
部署后把前端 `party-online.html` 也上传到 Render 或 GitHub Pages 即可。
