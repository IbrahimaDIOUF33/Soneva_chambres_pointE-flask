from flask import Flask, Response, render_template, request, redirect, url_for, flash
import psycopg2
import psycopg2.extras
import os
from datetime import datetime



# Authentification basique
def check_auth(username, password):
    return username == os.getenv("APP_USERNAME") and password == os.getenv("APP_PASSWORD")

def authenticate():
    return Response(
        'Accès refusé.\n'
        'Veuillez fournir un nom d’utilisateur et un mot de passe.', 401,
        {'WWW-Authenticate': 'Basic realm="Accès restreint"'})

def requires_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


def get_db_connection():
    conn = psycopg2.connect(os.environ['DATABASE_URL'], cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL non défini. Ce script ne peut être lancé qu'en ligne (Render).")

app = Flask(__name__)
app.secret_key = "secret123"

ETAT_LIBELLES = {
    "libre": "Libre",
    "reservee": "Réservée",
    "occupee": "Occupée"
}

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    # Création de la table (sans les colonnes supplémentaires ici)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS chambres (
        id SERIAL PRIMARY KEY,
        numero TEXT NOT NULL UNIQUE,
        etat TEXT CHECK(etat IN ('libre', 'reservee', 'occupee')) NOT NULL DEFAULT 'libre',
        client TEXT,
        datetime_debut TEXT,
        datetime_fin TEXT,
        observations TEXT
    )
    ''')

    # Ajout conditionnel des colonnes manquantes
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'chambres'")
    existing_columns = [col[0] for col in cursor.fetchall()]

    if 'tarif' not in existing_columns:
        cursor.execute("ALTER TABLE chambres ADD COLUMN tarif NUMERIC;")
    if 'identite' not in existing_columns:
        cursor.execute("ALTER TABLE chambres ADD COLUMN identite TEXT;")
    if 'agent' not in existing_columns:
        cursor.execute("ALTER TABLE chambres ADD COLUMN agent TEXT;")

    # Insertion de chambres 100 à 109 si elles n'existent pas
    for i in range(1, 11):
        cursor.execute("""
            INSERT INTO chambres (numero, etat)
            VALUES (%s, 'libre')
            ON CONFLICT (numero) DO NOTHING
        """, (f"{100+i}",))

    conn.commit()
    conn.close()



def format_duree(delta):
    total_seconds = int(delta.total_seconds())
    heures = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{heures}h{minutes:02d}"

def get_chambres():
    now = datetime.now()
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM chambres ORDER BY numero ASC")
    rows = cursor.fetchall()
    conn.close()

    chambres = []
    for row in rows:
        c = dict(row)
        debut = datetime.fromisoformat(c["datetime_debut"]) if c["datetime_debut"] else None
        fin = datetime.fromisoformat(c["datetime_fin"]) if c["datetime_fin"] else None

        c["debut_affichage"] = debut.strftime("%d/%m/%Y à %H:%M") if debut else ""
        c["fin_affichage"] = fin.strftime("%d/%m/%Y à %H:%M") if fin else ""
        c["etat_libelle"] = ETAT_LIBELLES.get(c["etat"], c["etat"])

        c["status_color"] = "lightgray"
        c["temps_restant"] = ""
        c["depassé"] = False

        if c["etat"] == "libre":
            c["status_color"] = "lightgreen"
        elif c["etat"] == "reservee":
            if debut and fin:
                if fin < now:
                    c["status_color"] = "lightblue"
                    c["depassé"] = True
                elif debut <= now <= fin:
                    c["status_color"] = "gray"
                    delta = fin - now
                    c["temps_restant"] = format_duree(delta)
        elif c["etat"] == "occupee":
            if debut and fin:
                if now <= fin:
                    c["status_color"] = "orange"
                    delta = fin - now
                    c["temps_restant"] = format_duree(delta)
                else:
                    c["status_color"] = "red"
                    c["depassé"] = True

        chambres.append(c)
    return chambres

@app.route('/')
@requires_auth
def index():
    chambres = get_chambres()
    return render_template('index.html', chambres=chambres)

@app.route('/reserver/<int:id>', methods=['GET', 'POST'])
def reserver(id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if request.method == 'POST':
        client = request.form['client']
        debut = request.form['datetime_debut']
        fin = request.form['datetime_fin']
        etat = request.form['etat']

        tarif_str = request.form.get('tarif')
        try:
            tarif = float(tarif_str.replace(',', '.')) if tarif_str else None
        except ValueError:
            flash("❌ Tarif invalide. Utilisez un nombre, par exemple 15000 ou 15000.00")
            return redirect(url_for('reserver', id=id))

        identite = request.form.get('identite') or None
        adresse = request.form.get('adresse') or None
        agent = request.form.get('agent') or None
        observations = request.form.get('observations', '')

        cursor.execute('''
            UPDATE chambres SET client = %s, datetime_debut = %s, datetime_fin = %s, etat = %s,
                               tarif = %s, identite = %s, adresse = %s, agent = %s, observations = %s
            WHERE id = %s
        ''', (client, debut, fin, etat, tarif, identite, adresse, agent, observations, id))

        conn.commit()
        conn.close()
        flash("✅ Réservation enregistrée avec succès.")
        return redirect(url_for('index'))

    cursor.execute("SELECT * FROM chambres WHERE id = %s", (id,))
    chambre = cursor.fetchone()
    conn.close()
    return render_template("reservation.html", chambre=chambre)

@app.route('/liberer/<int:id>', methods=['POST'])
def liberer(id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    # 📝 Sauvegarder l’état de la chambre dans historique
    cursor.execute('''
        INSERT INTO historique (
            chambre_numero, client, datetime_debut, datetime_fin,
            tarif, identite, agent, observations, adresse
        )
        SELECT numero, client, datetime_debut, datetime_fin,
               tarif, identite, agent, observations, adresse
        FROM chambres
        WHERE id = %s
    ''', (id,))

    # 🔄 Libérer la chambre (remise à zéro)
    cursor.execute('''
        UPDATE chambres SET
            etat = 'libre',
            client = NULL,
            datetime_debut = NULL,
            datetime_fin = NULL,
            observations = NULL,
            tarif = NULL,
            identite = NULL,
            adresse = NULL,
            agent = NULL
        WHERE id = %s
    ''', (id,))

    conn.commit()
    conn.close()
    flash(f"✅ Chambre {id} libérée.")
    return redirect(url_for('index'))

@app.route('/reservation-rapide/<int:id>', methods=['POST'])
def reservation_rapide(id):
    client = request.form['client']
    heure_debut = request.form['heure_debut']
    heure_fin = request.form['heure_fin']
    today = datetime.now().strftime("%Y-%m-%d")
    datetime_debut_str = f"{today}T{heure_debut}"
    datetime_fin_str = f"{today}T{heure_fin}"

    try:
        datetime_debut = datetime.fromisoformat(datetime_debut_str)
        datetime_fin = datetime.fromisoformat(datetime_fin_str)
    except ValueError:
        flash("❌ Heures incorrectes.")
        return redirect(url_for('index'))

    now = datetime.now()
    delay = (datetime_debut - now).total_seconds() / 60
    etat = "occupee" if delay <= 30 else "reservee"

    heure_min = datetime.strptime("06:00", "%H:%M").time()
    heure_max = datetime.strptime("23:59", "%H:%M").time()
    h_debut = datetime.strptime(heure_debut, "%H:%M").time()
    h_fin = datetime.strptime(heure_fin, "%H:%M").time()

    if not (heure_min <= h_debut <= heure_max and heure_min <= h_fin <= heure_max):
        flash("❌ Pour cet horaire, utilisez le bouton 'Réserver' classique.")
        return redirect(url_for('index'))

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute('''
                   
        UPDATE chambres SET
            client = %s,
            datetime_debut = %s,
            datetime_fin = %s,
            etat = %s,
            tarif = NULL,
            identite = NULL,
            adresse = NULL,
            agent = NULL,
            observations = NULL
        WHERE id = %s
    ''', (client, datetime_debut_str, datetime_fin_str, etat, id))

    conn.commit()
    conn.close()

    flash(f"✅ Réservation rapide enregistrée ({etat}).")
    return redirect(url_for('index'))

@app.route('/historique')
def historique():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM historique ORDER BY date_enregistrement DESC")
    historiques = cursor.fetchall()
    conn.close()
    return render_template("historique.html", historiques=historiques)

@app.route('/nettoyage/<int:id>', methods=['POST'])
def toggle_nettoyage(id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT nettoyee FROM chambres WHERE id = %s", (id,))
    current = cursor.fetchone()[0]
    nouveau = not current

    cursor.execute("UPDATE chambres SET nettoyee = %s WHERE id = %s", (nouveau, id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
