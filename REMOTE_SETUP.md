# Connect this folder to GitHub

The repository content is ready under `deepiri-node-base/`. Create the remote and push:

```bash
cd /path/to/deepiri-node-base
git init
git branch -M main
git add .
git commit -m "feat: initial deepiri-node-base image and GHCR publish workflow"
git remote add origin https://github.com/Team-Deepiri/deepiri-node-base.git
git push -u origin main
```

Replace the remote URL if your org or repo name differs.

After the first push, open **Actions** on GitHub and confirm **Publish deepiri-node-base** completes for all three matrix variants.
