from flask import Flask, render_template, request, redirect, session, send_from_directory, url_for, flash, jsonify
from werkzeug.utils import secure_filename
import mysql.connector
from mysql.connector import Error, pooling
import os
import time
import logging
from flask_compress import Compress
from flask_caching import Cache
from contextlib import contextmanager
from functools import wraps
import hashlib
import threading

# ════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN DE LOGGING
# ════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Importamos funciones externas
from recomendaciones import PREGUNTAS_RECOMENDACION, calcular_recomendacion, obtener_clubes_recomendados
from app_informes import generar_pdf, generar_excel

# ════════════════════════════════════════════════════════════════════
# INICIALIZACIÓN DE APP
# ════════════════════════════════════════════════════════════════════
app = Flask(__name__)
Compress(app)
cache = Cache(app, config={
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 5,
    "CACHE_THRESHOLD": 1000,
})

app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-CHANGE-in-production-2024')
IS_PRODUCTION = os.environ.get('RENDER') == 'true'

app.config['SESSION_COOKIE_SECURE'] = IS_PRODUCTION
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 hora (más tolerante para alumnos lentos)

# Configuración de imágenes
UPLOAD_FOLDER = 'static/uploads/clubes'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB máximo

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ════════════════════════════════════════════════════════════════════
# POOL DE CONEXIONES — CON RECONEXIÓN AUTOMÁTICA
# ════════════════════════════════════════════════════════════════════
db_pool = None
_pool_lock = threading.Lock()

def init_connection_pool():
    """Inicializa el pool con manejo robusto de errores para Aiven/Render."""
    global db_pool
    try:
        db_host     = os.environ.get('DB_HOST', '')
        db_user     = os.environ.get('DB_USER', 'avnadmin')
        db_password = os.environ.get('DB_PASSWORD', '')
        db_name     = os.environ.get('DB_NAME', 'defaultdb')
        db_port     = int(os.environ.get('DB_PORT', 18162))

        if not db_password:
            logger.critical("❌ DB_PASSWORD no configurada. Abortando pool.")
            return False
        if not db_host:
            logger.critical("❌ DB_HOST no configurada. Abortando pool.")
            return False

        # Plan pagado Render + Aiven: optimizado para velocidad
        db_pool = pooling.MySQLConnectionPool(
            pool_name="clubgest_pool",
            pool_size=10,               # Balance entre rapidez y no saturar Aiven
            pool_reset_session=True,
            host=db_host,
            user=db_user,
            password=db_password,
            database=db_name,
            port=db_port,
            connect_timeout=30,         # SSL handshake en Aiven puede tardar 20-30s
            connection_timeout=30,      # Tolerancia para latencia inicial
            autocommit=False,
            charset='utf8mb4',
            collation='utf8mb4_unicode_ci',
            auth_plugin='mysql_native_password',
            ssl_verify_cert=False,      # Aiven usa cert autofirmado
            ssl_verify_identity=False,
        )
        logger.info("✅ Pool optimizado: 10 conexiones por worker, timeouts 30s, charset utf8mb4")
        return True
    except Error as e:
        logger.critical(f"❌ Error inicializando pool: {e}")
        return False

def get_db_connection():
    """Obtiene conexión del pool con reinicio automático si falló."""
    global db_pool
    with _pool_lock:
        if db_pool is None:
            if not init_connection_pool():
                return None

    max_intentos = 3
    for intento in range(1, max_intentos + 1):
        try:
            conn = db_pool.get_connection()
            conn.ping(reconnect=True, attempts=1, delay=0)
            return conn
        except Error as e:
            logger.warning(f"⚠️ Intento {intento}/{max_intentos} de obtener conexión: {e}")
            if intento == max_intentos:
                logger.error("❌ No se pudo obtener conexión del pool")
                # Intentar reiniciar el pool como último recurso
                with _pool_lock:
                    db_pool = None
                    init_connection_pool()
                return None
            time.sleep(0.5 * intento)

@contextmanager
def get_db():
    """Context manager: obtiene conexión+cursor, hace commit o rollback automático."""
    conn = get_db_connection()
    if conn is None:
        raise Exception("Sin conexión a la base de datos. Intenta de nuevo en unos segundos.")

    cursor = conn.cursor(dictionary=True)
    try:
        yield conn, cursor
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"❌ Error en transacción BD: {e}")
        raise
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

def clear_route_cache():
    """Limpia la caché de rutas GET después de escrituras."""
    try:
        cache.clear()
    except Exception:
        pass

def cache_key_clubes():
    """Clave de caché por nivel para no mezclar listas entre cursos."""
    return f"clubes:{session.get('nivel', '0')}"

def cache_key_recomendacion():
    """Clave de caché por nivel y respuestas para no mezclar recomendaciones."""
    respuestas = session.get("respuestas_recomendacion", [])
    raw = "|".join(map(str, respuestas))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"recomendacion:{session.get('nivel', '0')}:{digest}"

# ════════════════════════════════════════════════════════════════════
# SEGURIDAD — ADMIN CON HASH SHA256
# ════════════════════════════════════════════════════════════════════
ADMIN_USERNAME      = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD_HASH = os.environ.get(
    'ADMIN_PASSWORD_HASH',
    hashlib.sha256('1234'.encode()).hexdigest()
)

def verify_admin_password(password: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin"):
            flash("Por favor inicia sesión primero", "warning")
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated_function

# ════════════════════════════════════════════════════════════════════
# RUTAS PÚBLICAS
# ════════════════════════════════════════════════════════════════════

@app.route('/imagenes/<path:filename>')
def serve_image(filename):
    return send_from_directory('imagenes', filename)

@app.route("/")
def inicio():
    return render_template("inicio.html")

@app.route("/formulario")
@cache.cached(timeout=10)
def formulario():
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles ORDER BY id_nivel")
            niveles = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades ORDER BY nombre_especialidad")
            especialidades = cursor.fetchall()
        return render_template("formulario.html", niveles=niveles, especialidades=especialidades)
    except Exception as e:
        logger.error(f"❌ /formulario: {e}")
        return render_template("error.html", error="Error al cargar el formulario. Intenta de nuevo.")

@app.route("/inscribirse", methods=["POST"])
def inscribirse():
    """Registra al estudiante tras validar el correo institucional."""
    nombre     = request.form.get("nombre", "").strip()
    apellido   = request.form.get("apellido", "").strip()
    correo     = request.form.get("correo", "").strip().lower()
    genero     = request.form.get("genero", "")
    nivel      = request.form.get("nivel", "")
    especialidad = request.form.get("especialidad", "")

    # Validaciones básicas
    if not all([nombre, apellido, correo, genero, nivel, especialidad]):
        return render_template("error.html", error="Todos los campos son obligatorios.")

    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles ORDER BY id_nivel")
            niveles = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades ORDER BY nombre_especialidad")
            especialidades = cursor.fetchall()

            if not correo.endswith("@donboscolatola.edu.ec"):
                return render_template("formulario.html",
                    error="Solo se permiten correos institucionales @donboscolatola.edu.ec",
                    niveles=niveles, especialidades=especialidades)

            cursor.execute(
                "SELECT id_estudiante FROM estudiantes WHERE correo_institucional = %s",
                (correo,)
            )
            existente = cursor.fetchone()

            if existente:
                # Verificar si ya está inscrito en un club
                cursor.execute(
                    "SELECT id_inscripcion FROM inscripciones WHERE id_estudiante = %s",
                    (existente['id_estudiante'],)
                )
                if cursor.fetchone():
                    return render_template("formulario.html",
                        error="Este correo ya está registrado y tiene un club asignado.",
                        niveles=niveles, especialidades=especialidades)
                else:
                    # Registrado pero sin club: reanudar sesión
                    session["id_estudiante"] = existente['id_estudiante']
                    session["nivel"] = nivel
                    return redirect("/clubes")

            cursor.execute(
                "INSERT INTO estudiantes (nombres, apellidos, correo_institucional, genero, id_nivel, id_especialidad) VALUES (%s,%s,%s,%s,%s,%s)",
                (nombre, apellido, correo, genero, nivel, especialidad)
            )
            session["id_estudiante"] = cursor.lastrowid
            session["nivel"] = nivel

        clear_route_cache()
        return redirect("/clubes")
    except Exception as e:
        logger.error(f"❌ /inscribirse: {e}")
        return render_template("error.html", error="Error al registrar. Por favor intenta de nuevo.")

@app.route("/cuestionario")
def cuestionario():
    if "id_estudiante" not in session:
        return redirect("/formulario")
    return render_template("preguntas.html", preguntas=PREGUNTAS_RECOMENDACION)

@app.route("/guardar_respuestas", methods=["POST"])
def guardar_respuestas():
    if "id_estudiante" not in session:
        return redirect("/formulario")

    respuestas = []
    for i in range(len(PREGUNTAS_RECOMENDACION)):
        r = request.form.get(f"pregunta_{i}")
        if r:
            respuestas.append(r)

    if len(respuestas) < len(PREGUNTAS_RECOMENDACION):
        return render_template("error.html", error="Por favor responde todas las preguntas.")

    session["respuestas_recomendacion"] = respuestas
    return redirect("/recomendacion_clubes")

@app.route("/recomendacion_clubes")
@cache.cached(timeout=5, key_prefix=cache_key_recomendacion, unless=lambda: "id_estudiante" not in session)
def recomendacion_clubes():
    if "id_estudiante" not in session:
        return redirect("/formulario")
    if "respuestas_recomendacion" not in session:
        return redirect("/cuestionario")

    try:
        with get_db() as (conn, cursor):
            nivel = int(session.get("nivel", 0))
            
            # ⚡ OPTIMIZADO: Una sola query con JOIN + GROUP BY
            cursor.execute("""
                SELECT
                    c.*,
                    n.nombre_nivel,
                    COUNT(i.id_inscripcion) AS inscritos,
                    GREATEST(0, c.cupo_maximo - COUNT(i.id_inscripcion)) AS cupos_restantes
                FROM clubes c
                JOIN niveles n ON c.id_nivel = n.id_nivel
                LEFT JOIN inscripciones i ON c.id_club = i.id_club
                WHERE c.id_nivel = %s AND c.activo = 1
                GROUP BY c.id_club
                ORDER BY c.nombre_club
            """, (nivel,))
            clubes_disponibles = cursor.fetchall()

        respuestas = session["respuestas_recomendacion"]
        recomendacion = calcular_recomendacion(respuestas)
        clubes_recomendados, otros_clubes = obtener_clubes_recomendados(
            clubes_disponibles, recomendacion["categoria"]
        )
        return render_template("recomendacion.html",
            recomendacion=recomendacion,
            clubes_recomendados=clubes_recomendados,
            otros_clubes=otros_clubes,
            clubes_disponibles=clubes_disponibles
        )
    except Exception as e:
        logger.error(f"❌ /recomendacion_clubes: {e}")
        return render_template("error.html", error="Error al cargar recomendaciones.")

@app.route("/clubes")
@cache.cached(timeout=5, key_prefix=cache_key_clubes, unless=lambda: "id_estudiante" not in session)
def clubes():
    if "id_estudiante" not in session:
        return redirect("/formulario")

    try:
        with get_db() as (conn, cursor):
            nivel = int(session.get("nivel", 0))
            
            # ⚡ OPTIMIZADO: Una sola query con JOIN + GROUP BY (más rápido en MySQL)
            cursor.execute("""
                SELECT
                    c.*,
                    COUNT(i.id_inscripcion) AS inscritos,
                    GREATEST(0, c.cupo_maximo - COUNT(i.id_inscripcion)) AS cupos_restantes
                FROM clubes c
                LEFT JOIN inscripciones i ON c.id_club = i.id_club
                WHERE c.id_nivel = %s AND c.activo = 1
                GROUP BY c.id_club
                ORDER BY c.nombre_club
            """, (nivel,))
            clubes_lista = cursor.fetchall()

        return render_template("clubes.html", clubes=clubes_lista, disponible=len(clubes_lista) > 0)
    except Exception as e:
        logger.error(f"❌ /clubes: {e}")
        return render_template("error.html", error="Error al cargar los clubes. Intenta de nuevo.")

# ════════════════════════════════════════════════════════════════════
# INSCRIPCIÓN — CON MANEJO ROBUSTO DE CONCURRENCIA (50+ ALUMNOS)
# ════════════════════════════════════════════════════════════════════

@app.route("/inscribir_club", methods=["POST"])
def inscribir_club():
    """
    Inscribe al estudiante en un club.
    Usa SELECT FOR UPDATE + SERIALIZABLE para garantizar que JAMÁS
    se supere el cupo, incluso con 50 alumnos enviando el formulario
    al mismo tiempo.
    """
    if "id_estudiante" not in session:
        return redirect("/formulario")

    estudiante_id = session.get("id_estudiante")
    club_raw      = request.form.get("club", "").strip()

    if not club_raw:
        return render_template("error.html", error="No seleccionaste ningún club. Vuelve atrás e intenta de nuevo.")

    try:
        club_id = int(club_raw)
    except (ValueError, TypeError):
        return render_template("error.html", error="Club inválido.")

    logger.info(f"📝 Inscripción — estudiante {estudiante_id}, club {club_id}")

    MAX_REINTENTOS = 3
    for intento in range(1, MAX_REINTENTOS + 1):
        conn = get_db_connection()
        if not conn:
            return render_template("error.html",
                error="Error de conexión. Por favor intenta de nuevo en unos segundos.")

        cursor = None
        try:
            cursor = conn.cursor(dictionary=True)

            # 🔒 Configurar SERIALIZABLE ANTES de empezar transacción (mysql-connector-python bug fix)
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            conn.autocommit = False
            conn.start_transaction()

            # 1. Verificar que el estudiante existe
            cursor.execute(
                "SELECT id_estudiante, id_nivel FROM estudiantes WHERE id_estudiante = %s",
                (estudiante_id,)
            )
            est = cursor.fetchone()
            if not est:
                conn.rollback()
                session.clear()
                return render_template("error.html", error="Tu sesión expiró. Por favor vuelve a registrarte.")

            # 2. Verificar que NO esté ya inscrito en algún club
            cursor.execute(
                "SELECT id_inscripcion FROM inscripciones WHERE id_estudiante = %s",
                (estudiante_id,)
            )
            if cursor.fetchone():
                conn.rollback()
                session.clear()
                return render_template("error.html", error="Ya tienes una inscripción activa. No puedes elegir otro club.")

            # 3. Bloquear la fila del club (FOR UPDATE) para contar en exclusiva
            cursor.execute(
                "SELECT id_club, id_nivel, cupo_maximo, nombre_club, activo FROM clubes WHERE id_club = %s FOR UPDATE",
                (club_id,)
            )
            club = cursor.fetchone()

            if not club:
                conn.rollback()
                return render_template("error.html", error="El club seleccionado no existe.")
            if not club['activo']:
                conn.rollback()
                return render_template("error.html", error="Este club ya no está disponible.")
            if club['id_nivel'] != est['id_nivel']:
                conn.rollback()
                return render_template("error.html", error="Este club no corresponde a tu nivel académico.")

            # 4. Contar inscritos dentro de la transacción bloqueada del club
            cursor.execute(
                "SELECT COUNT(*) AS total FROM inscripciones WHERE id_club = %s",
                (club_id,)
            )
            total_inscritos = cursor.fetchone()['total']
            cupos_disponibles = club['cupo_maximo'] - total_inscritos

            logger.info(f"📊 Club '{club['nombre_club']}': {total_inscritos}/{club['cupo_maximo']} inscritos (SERIALIZABLE en efecto)")

            if cupos_disponibles <= 0:
                conn.rollback()
                logger.critical(f"🚨 CLUB LLENO: intento de inscribir est {estudiante_id} en club {club_id} (0 cupos)")
                return render_template("error.html",
                    error=f"Lo sentimos, el club «{club['nombre_club']}» ya no tiene cupos disponibles. "
                          f"Por favor elige otro club.",
                    mostrar_volver=True
                )

            # 5. Insertar inscripción
            cursor.execute(
                "INSERT INTO inscripciones (id_estudiante, id_club, fecha_hora) VALUES (%s, %s, NOW())",
                (estudiante_id, club_id)
            )

            conn.commit()
            logger.info(f"✅ Inscripción exitosa — estudiante {estudiante_id} → club {club_id}")

            # Limpiar sesión
            session.pop("id_estudiante", None)
            session.pop("nivel", None)
            session.pop("respuestas_recomendacion", None)
            clear_route_cache()

            return render_template("exito.html")

        except mysql.connector.errors.DatabaseError as e:
            # Errno 1213 = Deadlock → reintentar
            if hasattr(e, 'errno') and e.errno == 1213:
                logger.warning(f"⚠️ Deadlock detectado, reintento {intento}/{MAX_REINTENTOS}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(0.15 * intento)
                continue
            else:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.error(f"❌ DatabaseError: {e}")
                return render_template("error.html", error="Error en la base de datos. Intenta de nuevo.")

        except mysql.connector.errors.IntegrityError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(f"⚠️ IntegrityError (posible duplicado): {e}")
            return render_template("error.html", error="Ya estabas inscrito o este club ya quedó lleno. Elige otra opción.")

        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"❌ Error inesperado en inscribir_club: {e}")
            return render_template("error.html", error="Error inesperado. Por favor intenta de nuevo.")

        finally:
            try:
                if cursor:
                    cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    # Se agotaron todos los reintentos (deadlocks continuos)
    logger.error(f"❌ Deadlocks repetidos para estudiante {estudiante_id}")
    return render_template("error.html",
        error="El sistema está muy ocupado en este momento. Espera 5 segundos e intenta de nuevo.")

# ════════════════════════════════════════════════════════════════════
# API PÚBLICA — CUPOS EN TIEMPO REAL
# ════════════════════════════════════════════════════════════════════

@app.route("/api/cupos/<int:club_id>")
@cache.cached(timeout=3)
def api_cupos(club_id):
    """Devuelve cupos disponibles para polling desde el frontend."""
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT c.cupo_maximo, COUNT(i.id_inscripcion) AS inscritos
                FROM clubes c
                LEFT JOIN inscripciones i ON c.id_club = i.id_club
                WHERE c.id_club = %s
                GROUP BY c.id_club
            """, (club_id,))
            r = cursor.fetchone()

        if not r:
            return jsonify({"error": "Club no encontrado"}), 404

        disponibles = max(0, r['cupo_maximo'] - r['inscritos'])
        return jsonify({
            "club_id":          club_id,
            "cupo_maximo":      r['cupo_maximo'],
            "inscritos":        r['inscritos'],
            "cupos_disponibles": disponibles,
            "porcentaje_lleno": round(r['inscritos'] / r['cupo_maximo'] * 100, 1) if r['cupo_maximo'] > 0 else 0,
            "lleno":            disponibles <= 0
        })
    except Exception as e:
        logger.error(f"❌ /api/cupos/{club_id}: {e}")
        return jsonify({"error": "Error interno"}), 500


@app.route("/api/cupos_todos")
@cache.cached(timeout=3)
def api_cupos_todos():
    """Una sola query para todos los clubes — reemplaza el polling individual."""
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT c.id_club, c.cupo_maximo, COUNT(i.id_inscripcion) AS inscritos
                FROM clubes c
                LEFT JOIN inscripciones i ON c.id_club = i.id_club
                WHERE c.activo = 1
                GROUP BY c.id_club
            """)
            rows = cursor.fetchall()

        result = {}
        for r in rows:
            disponibles = max(0, r['cupo_maximo'] - r['inscritos'])
            result[str(r['id_club'])] = {
                "cupo_maximo":       r['cupo_maximo'],
                "inscritos":         r['inscritos'],
                "cupos_disponibles": disponibles,
                "porcentaje_lleno":  round(r['inscritos'] / r['cupo_maximo'] * 100, 1) if r['cupo_maximo'] > 0 else 0,
                "lleno":             disponibles <= 0
            }
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ /api/cupos_todos: {e}")
        return jsonify({}), 500

# ════════════════════════════════════════════════════════════════════
# ADMINISTRACIÓN
# ════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("usuario", "").strip()
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and verify_admin_password(password):
            session["admin"] = True
            session.permanent = True
            flash("Inicio de sesión exitoso", "success")
            return redirect("/admin")
        else:
            flash("Usuario o contraseña incorrectos", "error")
            return render_template("login.html", error="Credenciales inválidas")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada correctamente", "info")
    return redirect("/")

@app.route("/admin")
@login_required
def admin():
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT c.*, n.nombre_nivel, COUNT(i.id_inscripcion) AS cupos_usados
                FROM clubes c
                JOIN niveles n ON c.id_nivel = n.id_nivel
                LEFT JOIN inscripciones i ON c.id_club = i.id_club
                GROUP BY c.id_club
                ORDER BY n.id_nivel, c.nombre_club
            """)
            clubes = cursor.fetchall()

            cursor.execute("SELECT * FROM niveles ORDER BY id_nivel")
            niveles = cursor.fetchall()

            cursor.execute("SELECT * FROM especialidades ORDER BY nombre_especialidad")
            especialidades = cursor.fetchall()

            cursor.execute("""
                SELECT e.nombres, e.apellidos, n.nombre_nivel, c.nombre_club,
                       DATE(i.fecha_hora) AS fecha, TIME(i.fecha_hora) AS hora
                FROM inscripciones i
                JOIN estudiantes e ON i.id_estudiante = e.id_estudiante
                JOIN clubes c ON i.id_club = c.id_club
                JOIN niveles n ON e.id_nivel = n.id_nivel
                ORDER BY i.fecha_hora DESC
                LIMIT 20
            """)
            historial = cursor.fetchall()

        return render_template("admin.html",
            clubes=clubes, niveles=niveles,
            especialidades=especialidades, historial=historial)
    except Exception as e:
        logger.error(f"❌ /admin: {e}")
        return render_template("error.html", error="Error al cargar el panel administrativo.")

@app.route("/crear_club", methods=["POST"])
@login_required
def crear_club():
    nombre      = request.form.get("nombre", "").strip()
    tutor       = request.form.get("tutor", "Por asignar").strip() or "Por asignar"
    descripcion = request.form.get("descripcion", "").strip()
    cupo        = request.form.get("cupo", "0")
    nivel       = request.form.get("nivel", "")

    try:
        cupo = max(1, int(cupo))
    except ValueError:
        flash("El cupo debe ser un número válido.", "error")
        return redirect("/admin")

    try:
        with get_db() as (conn, cursor):
            if tutor != "Por asignar":
                cursor.execute("SELECT id_club FROM clubes WHERE tutor = %s", (tutor,))
                if cursor.fetchone():
                    flash(f"El tutor '{tutor}' ya está asignado a otro club.", "error")
                    return redirect("/admin")

            filename = None
            if 'imagen' in request.files:
                file = request.files['imagen']
                if file and file.filename and allowed_file(file.filename):
                    filename = f"{int(time.time())}_{secure_filename(file.filename)}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

            cursor.execute("""
                INSERT INTO clubes (nombre_club, tutor, descripcion, imagen, cupo_maximo, id_nivel, activo)
                VALUES (%s, %s, %s, %s, %s, %s, 1)
            """, (nombre, tutor, descripcion, filename, cupo, nivel))

            flash(f"Club '{nombre}' creado exitosamente con {cupo} cupos.", "success")
            clear_route_cache()
    except mysql.connector.Error as err:
        if err.errno == 1062:
            flash("Error: El tutor ya está asignado a otro club.", "error")
        else:
            flash(f"Error en la base de datos: {err}", "error")
    except Exception as e:
        logger.error(f"❌ /crear_club: {e}")
        flash("Error al crear el club.", "error")

    return redirect("/admin")

@app.route("/editar_club/<int:club_id>", methods=["POST"])
@login_required
def editar_club(club_id):
    nombre         = request.form.get("nombre", "").strip()
    tutor          = request.form.get("tutor", "Por asignar").strip() or "Por asignar"
    descripcion    = request.form.get("descripcion", "").strip()
    nivel          = request.form.get("nivel", "")
    eliminar_imagen = request.form.get("eliminar_imagen") == "1"

    try:
        cupo = max(1, int(request.form.get("cupo", 1)))
    except ValueError:
        flash("El cupo debe ser un número válido.", "error")
        return redirect("/admin")

    try:
        with get_db() as (conn, cursor):
            if tutor != "Por asignar":
                cursor.execute(
                    "SELECT id_club FROM clubes WHERE tutor = %s AND id_club != %s",
                    (tutor, club_id)
                )
                if cursor.fetchone():
                    flash(f"El tutor '{tutor}' ya está asignado a otro club.", "error")
                    return redirect("/admin")

            cursor.execute("SELECT imagen FROM clubes WHERE id_club = %s", (club_id,))
            actual = cursor.fetchone()
            filename = actual['imagen'] if actual else None

            if eliminar_imagen and filename:
                _borrar_imagen(filename)
                filename = None

            if 'imagen' in request.files:
                file = request.files['imagen']
                if file and file.filename and allowed_file(file.filename):
                    if filename:
                        _borrar_imagen(filename)
                    filename = f"{int(time.time())}_{secure_filename(file.filename)}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

            cursor.execute("""
                UPDATE clubes
                SET nombre_club=%s, tutor=%s, descripcion=%s, imagen=%s, cupo_maximo=%s, id_nivel=%s
                WHERE id_club=%s
            """, (nombre, tutor, descripcion, filename, cupo, nivel, club_id))

            flash("Club actualizado exitosamente.", "success")
            clear_route_cache()
    except mysql.connector.Error as err:
        if err.errno == 1062:
            flash("Error: El tutor ya está asignado.", "error")
        else:
            flash(f"Error en la base de datos: {err}", "error")
    except Exception as e:
        logger.error(f"❌ /editar_club/{club_id}: {e}")
        flash("Error al actualizar el club.", "error")

    return redirect("/admin")

def _borrar_imagen(filename):
    """Elimina un archivo de imagen del disco de forma segura."""
    try:
        path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"⚠️ No se pudo borrar imagen {filename}: {e}")

@app.route("/desactivar/<int:club_id>")
@login_required
def desactivar(club_id):
    try:
        with get_db() as (conn, cursor):
            cursor.execute("UPDATE clubes SET activo = 0 WHERE id_club = %s", (club_id,))
            flash("Club desactivado.", "success")
    except Exception as e:
        logger.error(f"❌ /desactivar/{club_id}: {e}")
        flash("Error al desactivar.", "error")
    clear_route_cache()
    return redirect(request.headers.get("Referer") or "/admin")

@app.route("/activar/<int:club_id>")
@login_required
def activar(club_id):
    try:
        with get_db() as (conn, cursor):
            cursor.execute("UPDATE clubes SET activo = 1 WHERE id_club = %s", (club_id,))
            flash("Club activado.", "success")
    except Exception as e:
        logger.error(f"❌ /activar/{club_id}: {e}")
        flash("Error al activar.", "error")
    clear_route_cache()
    return redirect(request.headers.get("Referer") or "/admin")

@app.route("/eliminar_club/<int:club_id>")
@login_required
def eliminar_club(club_id):
    try:
        with get_db() as (conn, cursor):
            # Liberar a los estudiantes para que puedan re-inscribirse
            cursor.execute("SELECT id_estudiante FROM inscripciones WHERE id_club = %s", (club_id,))
            estudiantes = [r['id_estudiante'] for r in cursor.fetchall()]

            cursor.execute("DELETE FROM inscripciones WHERE id_club = %s", (club_id,))

            if estudiantes:
                placeholders = ','.join(['%s'] * len(estudiantes))
                cursor.execute(
                    f"DELETE FROM estudiantes WHERE id_estudiante IN ({placeholders})",
                    tuple(estudiantes)
                )

            cursor.execute("DELETE FROM clubes WHERE id_club = %s", (club_id,))
            flash("Club eliminado. Los estudiantes pueden volver a inscribirse.", "success")
            clear_route_cache()
    except Exception as e:
        logger.error(f"❌ /eliminar_club/{club_id}: {e}")
        flash("Error al eliminar el club.", "error")
    return redirect("/admin")

@app.route("/admin_inscripciones")
@login_required
def admin_inscripciones():
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles ORDER BY id_nivel")
            niveles_lista = cursor.fetchall()
            cursor.execute("SELECT * FROM clubes ORDER BY nombre_club")
            clubes_lista = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades ORDER BY nombre_especialidad")
            especialidades_lista = cursor.fetchall()

            def get_lista(id_nivel):
                cursor.execute("""
                    SELECT e.id_estudiante, e.nombres, e.apellidos, e.correo_institucional, e.genero,
                           e.id_nivel, e.id_especialidad, c.id_club, c.nombre_club, c.tutor,
                           esp.nombre_especialidad,
                           DATE(i.fecha_hora) AS fecha, TIME(i.fecha_hora) AS hora
                    FROM inscripciones i
                    JOIN estudiantes e  ON i.id_estudiante = e.id_estudiante
                    JOIN clubes c       ON i.id_club = c.id_club
                    JOIN especialidades esp ON e.id_especialidad = esp.id_especialidad
                    WHERE e.id_nivel = %s
                    ORDER BY e.apellidos, e.nombres
                """, (id_nivel,))
                return cursor.fetchall()

            return render_template("admin_inscripciones.html",
                primero=get_lista(1), segundo=get_lista(2), tercero=get_lista(3),
                niveles_lista=niveles_lista,
                clubes_lista=clubes_lista,
                especialidades_lista=especialidades_lista)
    except Exception as e:
        logger.error(f"❌ /admin_inscripciones: {e}")
        return render_template("error.html", error="Error al cargar el reporte.")

@app.route("/admin_clubes")
@login_required
def admin_clubes():
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles ORDER BY id_nivel")
            niveles_lista = cursor.fetchall()
            cursor.execute("SELECT * FROM clubes ORDER BY nombre_club")
            clubes_lista = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades ORDER BY nombre_especialidad")
            especialidades_lista = cursor.fetchall()

            def get_lista_club(id_nivel):
                cursor.execute("""
                    SELECT c.id_club, c.nombre_club, c.tutor,
                           e.id_estudiante, e.nombres, e.apellidos,
                           e.correo_institucional, e.genero, e.id_nivel, e.id_especialidad,
                           esp.nombre_especialidad,
                           i.id_inscripcion,
                           DATE(i.fecha_hora) AS fecha, TIME(i.fecha_hora) AS hora
                    FROM clubes c
                    LEFT JOIN inscripciones i ON c.id_club = i.id_club
                    LEFT JOIN estudiantes e   ON i.id_estudiante = e.id_estudiante
                    LEFT JOIN especialidades esp ON e.id_especialidad = esp.id_especialidad
                    WHERE c.id_nivel = %s
                    ORDER BY c.nombre_club, e.apellidos
                """, (id_nivel,))
                return cursor.fetchall()

            return render_template("admin_clubes.html",
                primero=get_lista_club(1), segundo=get_lista_club(2), tercero=get_lista_club(3),
                niveles_lista=niveles_lista,
                clubes_lista=clubes_lista,
                especialidades_lista=especialidades_lista)
    except Exception as e:
        logger.error(f"❌ /admin_clubes: {e}")
        return render_template("error.html", error="Error al cargar el reporte.")

@app.route("/editar_estudiante", methods=["POST"])
@login_required
def editar_estudiante():
    id_est    = request.form.get("id_estudiante")
    nombres   = request.form.get("nombres", "").strip()
    apellidos = request.form.get("apellidos", "").strip()
    correo    = request.form.get("correo", "").strip().lower()
    genero    = request.form.get("genero", "")
    id_niv    = request.form.get("id_nivel")
    id_esp    = request.form.get("id_especialidad")
    id_club   = request.form.get("id_club")

    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                UPDATE estudiantes
                SET nombres=%s, apellidos=%s, correo_institucional=%s,
                    genero=%s, id_nivel=%s, id_especialidad=%s
                WHERE id_estudiante=%s
            """, (nombres, apellidos, correo, genero, id_niv, id_esp, id_est))

            cursor.execute(
                "UPDATE inscripciones SET id_club=%s WHERE id_estudiante=%s",
                (id_club, id_est)
            )
            flash("Datos del estudiante actualizados correctamente.", "success")
    except Exception as e:
        logger.error(f"❌ /editar_estudiante: {e}")
        flash(f"Error al actualizar: {str(e)}", "error")

    return redirect(request.referrer or "/admin_clubes")

@app.route("/admin_Informes")
@login_required
def admin_informes():
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles ORDER BY id_nivel")
            niveles = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades ORDER BY nombre_especialidad")
            especialidades = cursor.fetchall()
            cursor.execute("SELECT id_club, nombre_club FROM clubes ORDER BY nombre_club")
            clubes = cursor.fetchall()
        return render_template("admin_Informes.html",
            niveles=niveles, especialidades=especialidades, clubes=clubes)
    except Exception as e:
        logger.error(f"❌ /admin_Informes: {e}")
        return render_template("error.html", error="Error al cargar panel de informes.")

# ════════════════════════════════════════════════════════════════════
# APIs ADMIN
# ════════════════════════════════════════════════════════════════════

@app.route("/buscar_estudiante")
@login_required
def buscar_estudiante():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify({"resultados": []})

    like = f"%{query}%"
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT e.id_estudiante, e.nombres, e.apellidos, e.correo_institucional,
                       e.genero, e.id_nivel, e.id_especialidad,
                       c.id_club, c.nombre_club, n.nombre_nivel
                FROM estudiantes e
                LEFT JOIN inscripciones i ON e.id_estudiante = i.id_estudiante
                LEFT JOIN clubes c        ON i.id_club = c.id_club
                LEFT JOIN niveles n       ON e.id_nivel = n.id_nivel
                WHERE e.nombres LIKE %s OR e.apellidos LIKE %s OR e.correo_institucional LIKE %s
                LIMIT 10
            """, (like, like, like))
            return jsonify({"resultados": cursor.fetchall()})
    except Exception as e:
        logger.error(f"❌ /buscar_estudiante: {e}")
        return jsonify({"error": str(e), "resultados": []}), 500

@app.route("/verificar_correo")
def verificar_correo():
    correo = request.args.get("correo", "").strip().lower()
    if not correo:
        return jsonify({"registrado": False})
    try:
        with get_db() as (conn, cursor):
            cursor.execute(
                "SELECT COUNT(*) AS total FROM estudiantes WHERE correo_institucional = %s",
                (correo,)
            )
            r = cursor.fetchone()
        return jsonify({"registrado": r["total"] > 0})
    except Exception as e:
        logger.error(f"❌ /verificar_correo: {e}")
        return jsonify({"error": str(e), "registrado": False}), 500

@app.route("/get_clubes_por_nivel")
@login_required
def get_clubes_por_nivel():
    id_nivel = request.args.get("id_nivel", "todos")
    try:
        with get_db() as (conn, cursor):
            if id_nivel == "todos":
                cursor.execute("SELECT id_club, nombre_club FROM clubes ORDER BY nombre_club")
            else:
                cursor.execute(
                    "SELECT id_club, nombre_club FROM clubes WHERE id_nivel = %s ORDER BY nombre_club",
                    (id_nivel,)
                )
            return jsonify({"clubes": cursor.fetchall()})
    except Exception as e:
        logger.error(f"❌ /get_clubes_por_nivel: {e}")
        return jsonify({"error": str(e), "clubes": []}), 500

def obtener_datos_informe(tipo, filtro=None, filtros_avanzados=None):
    sql = """
        SELECT e.nombres, e.apellidos, e.correo_institucional, e.genero,
               n.nombre_nivel, c.nombre_club, c.tutor, esp.nombre_especialidad
        FROM estudiantes e
        JOIN niveles n          ON e.id_nivel = n.id_nivel
        JOIN inscripciones i    ON e.id_estudiante = i.id_estudiante
        JOIN clubes c           ON i.id_club = c.id_club
        JOIN especialidades esp ON e.id_especialidad = esp.id_especialidad
    """
    params = []
    where  = []

    try:
        if tipo == 'avanzado' and filtros_avanzados:
            if filtros_avanzados.get('nivel') not in (None, 'todos'):
                where.append("e.id_nivel = %s")
                params.append(filtros_avanzados['nivel'])
            if filtros_avanzados.get('club') not in (None, 'todos'):
                where.append("c.id_club = %s")
                params.append(filtros_avanzados['club'])
            if filtros_avanzados.get('especialidad') not in (None, 'todos'):
                where.append("e.id_especialidad = %s")
                params.append(filtros_avanzados['especialidad'])
        elif filtro and filtro != 'todos':
            col = {'nivel': 'e.id_nivel', 'club': 'c.id_club', 'especialidad': 'e.id_especialidad'}.get(tipo)
            if col:
                where.append(f"{col} = %s")
                params.append(filtro)

        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.apellidos, e.nombres"

        with get_db() as (conn, cursor):
            cursor.execute(sql, tuple(params))
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"❌ obtener_datos_informe: {e}")
        return []

@app.route('/informe/<formato>/<tipo>')
@login_required
def informe(formato, tipo):
    try:
        if tipo == 'avanzado':
            filtros = {
                'nivel':       request.args.get('nivel', 'todos'),
                'club':        request.args.get('club', 'todos'),
                'especialidad': request.args.get('especialidad', 'todos')
            }
            datos = obtener_datos_informe('avanzado', filtros_avanzados=filtros)
            partes = []
            for clave, tabla, campo in [
                ('nivel', 'niveles', 'nombre_nivel'),
                ('club', 'clubes', 'nombre_club'),
                ('especialidad', 'especialidades', 'nombre_especialidad')
            ]:
                if filtros[clave] != 'todos':
                    try:
                        with get_db() as (conn, cursor):
                            cursor.execute(f"SELECT {campo} FROM {tabla} WHERE id_{clave} = %s", (filtros[clave],))
                            r = cursor.fetchone()
                            if r:
                                partes.append(r[campo])
                    except Exception:
                        pass
            titulo = f"Reporte Personalizado: {' + '.join(partes) if partes else 'Todos los registros'}"
            report_type = 'nivel'
        else:
            filtro = request.args.get('filtro', 'todos')
            datos = obtener_datos_informe(tipo, filtro)
            filtro_nombre = "Todos"
            if filtro != 'todos':
                tabla_map = {'nivel': ('niveles', 'nombre_nivel'), 'club': ('clubes', 'nombre_club'), 'especialidad': ('especialidades', 'nombre_especialidad')}
                if tipo in tabla_map:
                    tabla, campo = tabla_map[tipo]
                    try:
                        with get_db() as (conn, cursor):
                            cursor.execute(f"SELECT {campo} FROM {tabla} WHERE id_{tipo} = %s", (filtro,))
                            r = cursor.fetchone()
                            if r:
                                filtro_nombre = r[campo]
                    except Exception:
                        pass
            titulo = f"Reporte por {tipo.capitalize()}: {filtro_nombre}"
            report_type = tipo

        if formato == 'pdf':
            return generar_pdf(datos, titulo, report_type)
        return generar_excel(datos, titulo, report_type)

    except Exception as e:
        logger.error(f"❌ /informe/{formato}/{tipo}: {e}")
        flash("Error al generar el informe.", "error")
        return redirect("/admin_Informes")

# ════════════════════════════════════════════════════════════════════
# STATS PÚBLICAS — Para los contadores animados del inicio.html
# ════════════════════════════════════════════════════════════════════

@app.route("/stats_publicas")
def stats_publicas():
    """Devuelve estadísticas reales para los contadores de la página de inicio."""
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT COUNT(*) AS total FROM clubes WHERE activo = 1")
            clubes_activos = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) AS total FROM inscripciones")
            inscritos = cursor.fetchone()['total']

            cursor.execute("""
                SELECT GREATEST(0, SUM(c.cupo_maximo) - COUNT(i.id_inscripcion)) AS libres
                FROM clubes c
                LEFT JOIN inscripciones i ON c.id_club = i.id_club
                WHERE c.activo = 1
            """)
            resultado = cursor.fetchone()
            cupos_libres = int(resultado['libres']) if resultado['libres'] else 0

        return jsonify({
            "clubes_activos": clubes_activos,
            "inscritos":      inscritos,
            "cupos_libres":   cupos_libres
        })
    except Exception as e:
        logger.error(f"❌ /stats_publicas: {e}")
        # Devuelve ceros en vez de error 500 — el JS ya tiene fallback demo
        return jsonify({"clubes_activos": 0, "inscritos": 0, "cupos_libres": 0})

# ════════════════════════════════════════════════════════════════════
# HEALTH CHECK — Render lo usa para saber si el servicio está vivo
# ════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    """Health check para monitoreo."""
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT 1")
        return jsonify({"status": "ok", "db": "connected"}), 200
    except Exception as e:
        logger.error(f"❌ /health: {e}")
        return jsonify({"status": "error", "db": str(e)}), 503

# ════════════════════════════════════════════════════════════════════
# CONTEXT PROCESSOR — íconos automáticos
# ════════════════════════════════════════════════════════════════════

@app.context_processor
def utility_processor():
    def get_club_icon(nombre_club):
        n = nombre_club.lower()
        if any(x in n for x in ['compu', 'sist', 'prog', 'tec', 'robot', 'inform']):
            return '💻'
        if any(x in n for x in ['fut', 'socc', 'depor', 'gym', 'basquet', 'voley']):
            return '⚽'
        if any(x in n for x in ['mus', 'band', 'coro', 'guitar', 'canto']):
            return '🎵'
        if any(x in n for x in ['art', 'pint', 'dibuj', 'diseño']):
            return '🎨'
        if any(x in n for x in ['cien', 'quim', 'biol', 'fis', 'lab']):
            return '🔬'
        if any(x in n for x in ['lect', 'libr', 'bibli']):
            return '📚'
        if any(x in n for x in ['teat', 'actua', 'cine', 'drama']):
            return '🎭'
        if any(x in n for x in ['cocin', 'gastro', 'chef']):
            return '🍳'
        if any(x in n for x in ['foto', 'video', 'media']):
            return '📷'
        return '✨'
    return dict(get_club_icon=get_club_icon)

# ════════════════════════════════════════════════════════════════════
# MANEJADORES DE ERROR GLOBALES
# ════════════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", error="Página no encontrada (404)."), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"❌ Error 500: {e}")
    return render_template("error.html", error="Error interno del servidor. Intenta de nuevo."), 500

@app.errorhandler(413)
def too_large(e):
    return render_template("error.html", error="El archivo subido es demasiado grande (máx 5 MB)."), 413

@app.route('/error')
def error_page():
    return render_template("error.html", error="Ha ocurrido un error.")

# ════════════════════════════════════════════════════════════════════
# ARRANQUE
# ════════════════════════════════════════════════════════════════════

# ✅ NO inicializar pool aquí — Gunicorn hace fork y duplicaría conexiones
# El pool se crea dinámicamente en get_db_connection() cuando se necesita

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
