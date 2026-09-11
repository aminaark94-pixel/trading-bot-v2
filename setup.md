24/7 Multi-Bot Trading Matrix Setup Guide
Complete step-by-step instructions to get your free 24/7 automated crypto trading bot matrix running on GitHub Actions, Render, and Vercel.
Part 1: GitHub Repository Setup
Create a GitHub Repository:
Create a new public or private repository (e.g., trading-bot-hub).
Push Project Files:
code
Bash
git init
git add .
git commit -m "feat: complete trading bot architecture"
git remote add origin https://github.com/<YOUR_USER>/<YOUR_REPO>.git
git branch -M main
git push -u origin main
Enable GitHub Actions Permissions:
Go to your repository on GitHub.
Click Settings > Actions > General.
Under Workflow permissions, choose Read and write permissions.
Click Save.
Add Binance API Secrets:
Go to Settings > Secrets and variables > Actions > New repository secret.
Add BINANCE_API_KEY and BINANCE_SECRET_KEY (using Binance Testnet keys).
Part 2: Discord Bot & Render.com Setup (Free Alerts + Remote Control)
Create Discord Bot:
Visit the Discord Developer Portal.
Click New Application, give it a name, and go to Bot.
Reset and copy your Token.
Scroll down to Privileged Gateway Intents and enable Message Content Intent.
Go to OAuth2 > URL Generator:
Check bot and applications.commands.
Check Permissions: Send Messages, Embed Links, Read Message History.
Copy and paste the invite URL into your browser to invite the bot to your Discord server.
Right-click your desired alerts channel in Discord and click Copy ID (Enable Developer Mode in Discord settings if you don't see this).
Generate GitHub Personal Access Token (PAT):
In GitHub, go to your Profile Settings > Developer Settings > Personal access tokens > Tokens (classic).
Generate a new token with the repo scope selected. Copy the token (ghp_...).
Deploy to Render.com (Free Tier):
Go to Render.com and create a free account.
Click New + > Background Worker (or Web Service).
Connect your GitHub repo.
Configure:
Environment: Python 3
Build Command: pip install -r requirements.txt
Start Command: python discord_bot.py
Under Environment Variables, add:
DISCORD_BOT_TOKEN: (Your Discord bot token)
DISCORD_CHANNEL_ID: (Your channel ID)
GH_PAT: (Your GitHub personal access token)
GH_REPO: <username>/<repo_name>
Click Deploy Web Service / Worker.
Part 3: Deploy Dashboard on Vercel (Free Tier)
Go to Vercel.com and log in with GitHub.
Click Add New... > Project and select your trading bot repository.
Keep default settings and click Deploy.
Open the generated Vercel live URL.
In the top input bar, enter your GitHub Username, Repository Name, and branch (main), then click Connect GitHub Data.
The dashboard will automatically read signals/stats.json, signals/open.json, and signals/closed.json directly from your repository and refresh every 10 seconds.
Citations

https://github.com/git098/MFT_bot
https://github.com/RateCal/kznm-website