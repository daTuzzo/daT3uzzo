Your account does not get the Large model in the editor's assistant. The release note for version 1.5.13 says the Large model appears only when you sign in with an API key.

**What I found:**
- **Account.** The assistant is signed in as `user@example.com`, a personal account. The log shows `authMethod=personal`.
- **Model list.** The server picks the models for each account. At 23:25 tonight it sent this account 5 models: Small at Low, Medium and High, plus Medium and Code. The editor shows the three Small levels as one model with an effort setting. That is why your picker shows 3 models.
- **Large.** The client (`assist.exe` 1.5.13) knows the Large model. Its release note says: *"Added Large (Low, Medium, High) to `/model` when you connect with an `ASSIST_API_KEY`."*
- **The three models work.** The log shows them loaded with no error.

**To get Large:**
1. Create an API key on the provider's site, under "API keys".
2. In the editor, open Settings, then Assistant.
3. Set **Sign-in method** to API key.
4. Paste the key into the **API key** field.
5. Wait for the check to finish, then open the model picker. Large should be in the list.

Warning: an API key bills each request to your account. Your monthly plan does not cover it.

To check the result, open the model picker after step 5, or look in `C:\Users\me\.assist\models.json`. A model with an id like `large-2` must be in its `models` list.
