# Connect this folder to GitHub

The repository content is ready under `deepiri-suite/`. Create the remote and push:

```bash
cd /path/to/deepiri-suite
git init
git branch -M main
git add .
git commit -m "feat: initial deepiri-suite image and GHCR publish workflow"
git remote add origin https://github.com/Team-Deepiri/deepiri-suite.git
git push -u origin main
```

Replace the remote URL if your org or repo name differs.

After the first push, open **Actions** on GitHub and confirm **Publish deepiri-suite** completes for all three matrix variants.
