# Checklista: Fa databasen att fungera pa Render

Denna checklista ar gjord for ditt lage just nu: dry-run fungerade, riktig korning failade pga fel DATABASE_URL.

## Del A - Forberedelser (lokalt i CMD)

1. Stang gamla CMD-fonster och oppna ett nytt.
2. Gaa till projektmappen:
   - cd C:\Users\lindf\OneDrive\Finans\AI-bors
3. [x] Bekrafta att filerna finns:
   - db_schema.sql
   - migrate_to_postgres.py
4. [x] Bekrafta att psycopg finns installerat:
   - python -c "import psycopg; print('psycopg OK')"

## Del B - Hamta ratt URL i Render

1. Oppna Render Dashboard.
2. Oppna databasen ai-bors-users-db.
3. Scrolla till Connections.
4. Kopiera External Database URL (inte Internal) for lokal CMD-korning.

## Del C - Satt DATABASE_URL ratt i CMD

1. Klistra in den kopierade URL:en i CMD sa har:
   - SET "DATABASE_URL=PASTA_IN_HELA_EXTERNAL_URL_HAR"
2. Kontrollera att variabeln ar satt:
   - echo %DATABASE_URL%
3. Viktigt:
   - Om du ser texten "klistra_in_din_External_Database_URL_har" har du inte klistrat in riktig URL.

## Del D - Testa anslutning fore migrering

1. Kor snabb DB-test:
   - python -c "import os, psycopg; psycopg.connect(os.environ['DATABASE_URL']).close(); print('DB OK')"
2. Om du inte far "DB OK", stoppa och felsok URL/brandvagg/tecken i URL.

## Del E - Kor migrering

1. Kor dry-run igen:
   - python migrate_to_postgres.py --dry-run
2. Kor riktig migrering:
   - python migrate_to_postgres.py
3. Forvantat resultat:
   - "Migration complete."

## Del F - Verifiera att tabeller och data finns (utan psql)

1. Kor:
   - python -c "import os, psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); cur=c.cursor(); cur.execute('select count(*) from users'); print('users:', cur.fetchone()[0]); cur.execute('select count(*) from trades'); print('trades:', cur.fetchone()[0]); cur.execute('select count(*) from user_settings'); print('settings:', cur.fetchone()[0]); c.close()"

## Del G - Koppla appen pa Render

1. I Render: ga till Apps -> din web service (inte databasen).
2. Gaa till Environment.
3. Satt DATABASE_URL till Internal Database URL.
4. Save Changes.
5. Deploya om web service.

## Del H - Funktionstest efter deploy

1. Logga in med ett befintligt konto.
2. Kontrollera att portfolio syns.
3. Kontrollera att Min Trend/inställningar sparas.
4. Skapa testanvandare och verifiera att den finns kvar efter ny deploy.

## Vanliga fel och snabb fix

1. Fel: missing "=" after ...
   - Orsak: DATABASE_URL innehaller platshallare eller trasig URL.
   - Fix: satt om med riktig External URL.
2. Fel: psql is not recognized
   - Orsak: psql ej installerat.
   - Fix: anvand Python-kommandon i checklistan (du behover inte psql).
3. Dry-run funkar men riktig korning failar
   - Orsak: dry-run ansluter inte till DB.
   - Fix: kontrollera Del C + Del D.

## Status pa "fardig migreringskod"

Ja, migreringskoden ar skapad:
- migrate_to_postgres.py
- db_schema.sql

Det som aterstar ar att du kor Del B till Del G klart med riktig URL.
