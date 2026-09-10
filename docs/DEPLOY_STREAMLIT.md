# Putting the app online with Streamlit — step by step

No coding. Two websites: **GitHub** (holds the files) and **Streamlit**
(runs them). ~20 minutes.

Have ready: the `RetrievalStudio-v0.1.2-beta.zip`, and — if you want
lead capture — your Google Sheet `/exec` webhook URL (see
`leads_google_sheet.gs`).

---

## Part A — Put the files on GitHub

1. **Make a GitHub account** (skip if you have one): open
   https://github.com/signup and follow the prompts.
2. **Unzip** `RetrievalStudio-v0.1.2-beta.zip` on your computer. You'll
   get a folder with `app.py`, `core`, `requirements.txt`, etc. inside.
3. **Create an empty repository:** open https://github.com/new
   - **Repository name:** `Retrieval-Studio`
   - Choose **Private** (recommended — it collects emails).
   - Leave every checkbox **unticked** (no README, no .gitignore, no licence
     — the zip already has them).
   - Click **Create repository**.
4. On the next page, click the link **"uploading an existing file"**
   (or go to `https://github.com/YOUR-USERNAME/Retrieval-Studio/upload/main`).
5. **Open the unzipped folder**, select **everything inside it**
   (Ctrl+A), and **drag it onto the GitHub upload page.** Wait for the list
   to finish loading.
   - If Windows hides the folders starting with a dot (`.streamlit`,
     `.github`): don't worry, the app still runs without them.
6. Scroll down, click **Commit changes**. Your code is now on GitHub.

---

## Part B — Run it on Streamlit

7. Open https://share.streamlit.io and click **Sign in** → **Continue with
   GitHub** → **Authorize**.
8. Click **Create app** (top right) → choose **"Deploy a public app from a
   GitHub repo"** (works for private repos too, since you're the owner).
9. Fill in:
   - **Repository:** `YOUR-USERNAME/Retrieval-Studio`
   - **Branch:** `main`
   - **Main file path:** `app.py`
   - (Optional) **App URL:** pick a name, e.g. `fanout-studio`.
10. Click **Advanced settings** → set **Python version 3.12**.
11. Still in Advanced settings, find the **Secrets** box and paste this
    (edit the values; delete the two lead lines if you're not using the
    Google Sheet):
    ```toml
    RETRIEVAL_MODE = "hosted"
    RETRIEVAL_ACCESS_CODE = "pick-a-code"
    RETRIEVAL_LEADS_WEBHOOK = "https://script.google.com/macros/s/…/exec"
    RETRIEVAL_LEADS_TOKEN = "pick-a-secret"
    ```
    **Do NOT** put an `OPENAI_API_KEY` here — each visitor types their own.
12. Click **Deploy**. Wait 2–5 minutes for it to build. When it's ready you
    get a URL like `https://fanout-studio.streamlit.app`.

---

## Part C — Check it works

13. Open your app URL. You should see the **access-code gate** asking for
    name, email and the code. Enter them + the code you chose → you're in.
14. If you set up the Google Sheet, that signup should appear as a new row
    on the **Leads** tab.
15. Share the URL **and** the access code only with the people you want
    (prospects, colleagues). That's your gated beta.

## Changing settings later
- **Edit secrets / access code:** on https://share.streamlit.io open your
  app → the **⋮** menu → **Settings** → **Secrets** → edit → save (the app
  restarts).
- **Update the app after a code change:** upload the new files to the same
  GitHub repo; Streamlit redeploys automatically.

## Notes
- Streamlit's free tier sleeps the app after inactivity; the first visit
  after a nap takes ~30s to wake. Fine for a beta.
- Storage is temporary: each visitor's studies are deleted when their
  session expires — that's by design. Leads persist because they go to your
  Google Sheet.
- Keep the app **gated** (access code on) until the public-launch items in
  `HOSTING.md` are built.
