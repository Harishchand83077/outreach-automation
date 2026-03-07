# Push This Project to a New GitHub Repo

Follow these steps to create a new GitHub repository and push your committed code.

---

## Step 1: Create the repository on GitHub

1. Open **https://github.com/new**
2. **Repository name:** e.g. `funding-outreach-automation` (or any name you like)
3. **Description (optional):** e.g. `Automated funding outreach using LangGraph and async Python`
4. Choose **Public** (or Private if you prefer)
5. Do **not** check "Add a README file", "Add .gitignore", or "Choose a license" (you already have these in the project)
6. Click **Create repository**

---

## Step 2: Add the remote and push (PowerShell)

Replace `YOUR_USERNAME` and `YOUR_REPO` with your GitHub username and the repo name you chose.

```powershell
cd c:\Users\Mi\Downloads\automation

git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git branch -M main
git push -u origin main
```

**Example:** If your username is `johndoe` and the repo is `funding-outreach-automation`:

```powershell
git remote add origin https://github.com/johndoe/funding-outreach-automation.git
git branch -M main
git push -u origin main
```

---

## Step 3: If you use SSH instead of HTTPS

If you use SSH keys with GitHub:

```powershell
git remote add origin git@github.com:YOUR_USERNAME/YOUR_REPO.git
git branch -M main
git push -u origin main
```

---

## Notes

- **`.env` is not committed** (it is in `.gitignore`). Your API keys and passwords stay local. Anyone who clones the repo should copy `.env.example` to `.env` and fill in their own values.
- **`venv/` and `*.db` are not committed** (ignored). After cloning, users create their own venv and run `pip install -r requirements.txt`.
- If GitHub asks for login, use your GitHub username and a **Personal Access Token** (not your account password) when using HTTPS. Create one at: https://github.com/settings/tokens
