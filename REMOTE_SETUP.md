# Connect this folder to GitHub

The repository content is ready under `deepiri-toolchain/`. Create the remote and push:

```bash
cd /path/to/deepiri-toolchain
git init
git branch -M main
git add .
git commit -m "feat: initial deepiri-toolchain image and GHCR publish workflow"
git remote add origin https://github.com/Team-Deepiri/deepiri-toolchain.git
git push -u origin main
```

Replace the remote URL if your org or repo name differs.

After the first push, open **Actions** on GitHub and confirm **Publish deepiri-toolchain** completes for all three matrix variants.
