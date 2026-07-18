# Turn on live Google suggestions in the rep typeahead

The code is already deployed and inert. One environment variable on Render switches it on. Ten minutes, one time.

## What you get
Rep types a venue name on the New Account flow and live Google Places suggestions appear (marked with a Google badge), alongside the book, the AGCO licence universe, and the map layer. We use Google for display and form-autofill only, which their terms permit; nothing from Google is stored except the name and address the rep confirms.

## Cost
Google's free tier covers about 10,000 autocomplete calls per month, far more than the reps will type. A billing account must be attached (Google requires it), but usage stays inside the free cap. Set a budget alert at $5 so a surprise is impossible.

## Steps (you, ~10 minutes)

1. Go to console.cloud.google.com and sign in with your Google account.
2. Create a project (top bar > project picker > New Project). Name it `anu-imports`.
3. Attach billing when prompted (Billing > Link a billing account). Then go to Billing > Budgets and alerts > Create budget, set $5.
4. Enable the API: search "Places API (New)" in the top search bar, open it, press Enable.
5. Create the key: menu > APIs & Services > Credentials > Create credentials > API key. Copy it.
6. Restrict the key (important): click the new key > under API restrictions choose "Restrict key" > tick only "Places API (New)" > Save.
7. Paste it into Render: dashboard.render.com > anu-imports-tracker service > Environment > Add Environment Variable:
   - Key: `GOOGLE_PLACES_KEY`
   - Value: the key you copied
   Save. Render restarts the service by itself (about 2 minutes).

## How to know it worked
Open the app, HORECA > New Account, type any venue name. Google-badged suggestions appear within a second. Or tell Claude "key is in" and it will verify from the API side and confirm.

## If you ever want it off
Delete the `GOOGLE_PLACES_KEY` variable on Render. The typeahead falls back to the free map layer instantly. Nothing else changes.
