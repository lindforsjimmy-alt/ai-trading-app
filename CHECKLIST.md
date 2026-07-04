# Checklista för att åtgärda Dashboard & Portfolio

1. Fix dashboard render:
   - Uppdatera `app.py` så att den skickar listor `stocks`, `crypto`, `wait` till mallen istället för HTML-strängar.
   - Fil: [app.py](app.py)

2. Fix dashboard anchors:
   - Rätta till felaktiga/stray `</a>` i [Templates/dashboard.html](Templates/dashboard.html).

3. Fix buy/news links:
   - Korrigera `get_buy_link()` och nyhetslänken i `render_asset()` så att de använder giltiga `<a href="..."></a>`.
   - Fil: [app.py](app.py)

4. Fix portfolio render:
   - Åtgärda `portfolio_page()` så att den inte försöker rendera en saknad `portfolio.html` eller säkerställ att variabler finns.
   - Fil: [app.py](app.py)

5. Remove unused import:
   - Ta bort eller använd `from main import signal` i `app.py` för att undvika döda importer.
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

13. Export checklist file:
    - Denna fil (`CHECKLIST.md`) skapad för nedladdning och steg-för-steg arbete.

---

Vill du att jag också automatiskt skapar en PR med dessa ändringar eller kör lint och syntaxkontroller nu?