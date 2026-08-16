# Move to a Clean GitHub Repository

## Recommended repository name

`DEP-NESSO-SAFETY-CLEAN`

## Upload

Create a new empty GitHub repository, then upload the contents of the clean project folder.

Do not upload:
- `.env`
- any `secrets.h`
- Supabase passwords
- Telegram bot tokens
- Render ingest keys
- raw private datasets unless you deliberately want them public

The supplied `.gitignore` blocks the main secret files.

## Render

The safest migration is to keep the existing Render service and change its connected GitHub repository to the clean repository. This preserves the public service URL and avoids changing the Arduino API URL.

After the repository switch, verify the environment variables in Render and redeploy.
