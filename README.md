# FUSED Tender Intelligence

A private daily dashboard of electrical compliance tenders (PAT, EICR, fixed wire, emergency lighting, EV inspection) from Find a Tender, Contracts Finder, Public Contracts Scotland and Sell2Wales. It has a shared Bid / No Bid review.

- **Runs:** every morning on GitHub Actions (free).
- **Hosted:** on Cloudflare Pages, behind Cloudflare Access. Only the email addresses you allow can open it (free for up to 50 users).
- **Decisions:** stored in Supabase, shared between everyone signed in with the team passcode.

| File | What it is |
|---|---|
| `fetch_and_score.py` | Fetches, scores and builds the dashboard |
| `.github/workflows/daily-refresh.yml` | The daily job: runs the script, publishes to Cloudflare |
| `supabase_setup.sql` | Database setup for Bid / No Bid decisions (already run) |
| `.github/workflows/renewals-backfill.yml` | One-off scan of past award notices for the Renewals list |
| `requirements.txt` | Python packages |

---

## Setup (about 30 minutes, one time)

### 1. Create the GitHub repo
1. On GitHub, click **New repository**. Name it, for example, `fused-tenders`.
   **Public is fine and recommended.** The repo only holds the code. The dashboard itself is never stored here, and public repos get unlimited free Actions minutes. Choose Private if you prefer, but private repos have 2,000 free minutes a month and a daily run can use a lot of them.
2. Upload `fetch_and_score.py`, `supabase_setup.sql`, `requirements.txt`, `README.md` and `.gitignore` (**Add file → Upload files**).
3. Add the workflow: click **Add file → Create new file**, type the name `.github/workflows/daily-refresh.yml` (the slashes create the folders), paste in the contents of `daily-refresh.yml` and click **Commit**.
   *(Uploading the `.github` folder directly often fails because Mac and Windows hide folders starting with a dot.)*

### 2. Supabase (shared decisions) - already done
The Supabase project **Tenders** is set up, and its URL and public key are built into the script. The only thing left is to set the team passcode. In Supabase, open **SQL Editor → New query**, run this line with your own passcode, and share the passcode with Andy:
```sql
update public.team_passcode set hash = extensions.crypt('YOUR-PASSCODE', extensions.gen_salt('bf'));
```
Run the same line again whenever you want to change the passcode. (`supabase_setup.sql` is kept for reference only; it has already been run.)

### 3. Cloudflare (hosting)
1. Create a free account at dash.cloudflare.com.
2. **Account ID:** go to **Workers & Pages**. The Account ID is on the right-hand side, or in the account menu under **Copy account ID**.
3. **API token:** click your profile icon, then **My Profile → API Tokens → Create Token → Create Custom Token**.
   - Name: `GitHub tender deploy`.
   - Permissions: **Account → Cloudflare Pages → Edit**.
   - Click **Continue → Create Token** and copy the token. It is shown only once.

### 4. Add the secrets to GitHub
In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add these two:

| Name | Value |
|---|---|
| `CLOUDFLARE_API_TOKEN` | The Cloudflare token |
| `CLOUDFLARE_ACCOUNT_ID` | The Cloudflare Account ID |

### 5. First run
1. Go to the repo's **Actions** tab, open **Daily tender refresh** and click **Run workflow**.
2. The run can take a while, because it walks every notice in the feeds. When it finishes, open the **Publish to Cloudflare Pages** step. The log shows your site address, usually `https://fused-tenders.pages.dev`.

### 6. Make it private (do this straight after the first run)
1. In Cloudflare, open **Zero Trust**. The first time, pick a team name and the **Free** plan. A card may be needed, but you won't be charged.
2. Go to **Access → Applications → Add an application → Self-hosted**.
   - Application name: `FUSED Tenders`.
   - Domain: your address from step 5, for example `fused-tenders.pages.dev`. Add a second domain `*.fused-tenders.pages.dev` so the per-run preview links are covered too.
   - Policy: name it `Fused team`, Action **Allow**, Include **Emails**, then enter your email and Andy's.
   - Login method: **One-time PIN**. This is on by default: Cloudflare emails a code to sign in.
3. Save. Then open the site in a private/incognito window. You should get a Cloudflare sign-in page first.

Until this step is done, the page is reachable by anyone who knows the address. It only contains public tender notices, and Bid / No Bid decisions still need the passcode.

---

## Renewals (contracts ending soon)
The **Renewals** filter lists awarded contracts for Fused services that end in the next 18 months, with the current supplier, value and end date. Contracts ending in 6–12 months are marked **Prime window**. Use **Pursue / Ignore** to track which ones to chase.
- Needs the `SUPABASE_SERVICE_KEY` repo secret (the Supabase **secret** key). Never put it in the code.
- **History, one-off:** go to **Actions → Renewals backfill (one-off) → Run workflow** with the boxes left blank. Each run scans up to about 5 hours of past award notices (newest month first, back to 2021) and remembers where it stopped. Run it again until the log says `Backfill already complete`.
- After that, the daily refresh adds new award notices automatically.

## Day to day
- **Opening the site:** enter your email, then the code Cloudflare sends you. You stay signed in for 24 hours by default (you can change this in the Access application settings).
- **The first time on each device:** enter your name and the team passcode in the orange sign-in bar.
- **Refresh now:** **Actions → Daily tender refresh → Run workflow**.
- **Add a person:** add their email to the Access policy and give them the passcode.
- **Change the passcode:** run the `update public.team_passcode ...` line from step 2 in the Supabase SQL Editor.
- **Change the run time:** edit the `cron` line in the workflow. Times are UTC.
