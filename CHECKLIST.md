# Checklista för att åtgärda Dashboard & Portfolio

1. ~~Fix dashboard render:~~
   - Klar i kod: `app.py` skickar nu listor `stocks`, `crypto`, `wait` till mallen.
   - Fil: [app.py](app.py)

2. ~~Fix dashboard anchors:~~
   - Klar i kod: de uppenbara stray-ankarna i [Templates/dashboard.html](Templates/dashboard.html) är åtgärdade.

3. ~~Fix buy/news links:~~
   - Klar i kod: `get_buy_link()` och nyhetslänken bygger giltiga `<a href="..."></a>`.
   - Fil: [app.py](app.py)

4. ~~Fix portfolio render:~~
   - Klar i kod: `portfolio_page()` renderar nu samma dashboard-template med rätt variabler.
   - Fil: [app.py](app.py)

5. ~~Remove unused import:~~
   - Klar i kod: `from main import signal` finns inte längre i [app.py](app.py).
   - Fil: [app.py](app.py)

6. Consolidate trade logic:
   - Granska och slå ihop duplicerad `buy`/`sell`-logik mellan `main.py` och `app.py` för undvikande av inkonsistens.
   - Fil: [main.py](main.py), [app.py](app.py)

7. Add logging:
   - Ersätt tysta `except:` med loggning (print eller logging) i kritiska funktioner för bättre felsökning.
   - Fil: [app.py](app.py)

8. Normalize price formatting:
   - Justera `format_price()` så att den visar rimligt antal decimaler utan konstiga extra-nollor.
   - Fil: [app.py](app.py)

9. Create portfolio template (valfritt):
   - Antingen återanvänd `Templates/dashboard.html` eller skapa en ny `Templates/portfolio.html` för portfoliovyn.

10. Run lint and syntax checks:
    - Kör `python -m pyflakes .` eller `flake8` och fixa syntax-/stilfel.

11. Run app and verify UI:
    - Starta appen och testa `Dashboard` och `Portfolio` i webbläsaren. Verifiera buy/sell knappar och AI-analys.

12. Commit changes:
    - Gör commit med tydligt meddelande efter varje logisk förändring.

13. ~~Export checklist file:~~
   - Klar: denna fil (`CHECKLIST.md`) finns redan och används som arbetslista.

---

Vill du att jag också automatiskt skapar en PR med dessa ändringar eller kör lint och syntaxkontroller nu?

---

# Checklista: Migrera från filer till Postgres på Render

1. [x] Bekräfta dependency:
   - `psycopg[binary]` finns i `requirements.txt`.

2. [x] Kontrollera filer som ska migreras:
   - `stock_data/users.txt`
   - `stock_data/pending.txt`
   - `stock_data/admins.txt`
   - `stock_data/my_trades.txt`
   - `stock_data/user_settings.json`

3. [x] Säkerställ att schemafil och script finns:
   - `db_schema.sql`
   - `migrate_to_postgres.py`

4. Hämta rätt URL från Render:
   - Lokal körning i CMD/VS Code: använd **External Database URL**.
   - Körning inne på Render Web Service: använd **Internal Database URL**.

5. Sätt lokal variabel i CMD (engång per fönster):
   - `SET "DATABASE_URL=<EXTERNAL_DATABASE_URL>"`

6. Testa migrering utan skrivning:
   - `python migrate_to_postgres.py --dry-run`

7. Kör riktig migrering:
   - `python migrate_to_postgres.py`

8. Verifiera anslutning via Python (utan psql):
   - `python -c "import os, psycopg; psycopg.connect(os.environ['DATABASE_URL']).close(); print('DB OK')"`

9. Sätt `DATABASE_URL` i Render Web Service:
   - Gå till Web Service -> Environment.
   - Lägg in `DATABASE_URL` = **Internal Database URL**.
   - Save changes.

10. Deploya om Web Service:
   - Kontrollera logs för uppstarts- och DB-fel.

11. Funktionstest efter deploy:
   - Login
   - Registrering
   - Portfolio visas korrekt
   - Min Trend-inställningar sparas

12. Behåll filer som backup tills allt verifierats:
   - Ta inte bort textfiler förrän produktion är stabil.

---

# Checklista: Blockera köp-rekommendationer för sålda trades med förlust

1. Lägg till en ny inställning i portfolio-formuläret:
   - Checkbox precis ovanför SÄLJ-blocket.
   - Text: "Blocka trades sålda med förlust".
   - Lägg till en liten inforuta som förklarar att AI inte kommer att rekommendera trades som sålts med förlust när rutan är ibockad.

2. Spara inställningen per användare i databasen på Render:
   - Lägg till en boolean-kolumn i `user_settings`, t.ex. `block_loss_sells`.
   - Se till att `db_save_settings()` och `db_load_settings()` hanterar fältet.
   - Behåll fallback för fil-läge om databasen inte används.

3. Spåra säljtillfällen så att förlustaffärer kan identifieras:
   - Spara avslutade säljposter i DB eller separat historik så man kan se inköpspris vs säljpris.
   - Säkerställ att ticker, användare, qty, buy price och sell price finns att läsa senare.

4. Filtrera bort förlustsålda ticker i AI-köpen när inställningen är på:
   - Exkludera dessa från köp-listor för aktier och krypto.
   - Håll logiken så att samma ticker kan komma tillbaka om användaren avmarkerar rutan.

5. Visa valet i portföljen ovanför SÄLJ:
   - Placera inställningen exakt ovanför SÄLJ-sektionen i [Templates/dashboard.html](Templates/dashboard.html).
   - Se till att den sparas via samma POST-formulär som övriga portfolio-inställningar.

6. Verifiera i Render:
   - Deploya om efter schemaändring.
   - Testa att en förlustsåld ticker inte återkommer som köp när rutan är ibockad.
   - Testa att den kan rekommenderas igen när rutan tas bort.

7. Kom ihåg vid paus:
   - Om arbetet avbryts, börja med att kontrollera DB-schema, portfolio-formuläret och köpfiltreringen innan vidare ändringar.