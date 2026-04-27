from flask import Flask, render_template, request, redirect, session, send_from_directory, url_for, flash, jsonify
from werkzeug.utils import secure_filename
import mysql.connector
from mysql.connector import Error, pooling
import os
import time
import logging
from flask_compress import Compress
from contextlib import contextmanager
from functools import wraps
import hashlib
import secrets

# Configurar logging para monitoreo en Render
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Importamos las funciones que creamos para las recomendaciones
from recomendaciones import PREGUNTAS_RECOMENDACION, calcular_recomendacion, obtener_clubes_recomendados
from app_informes import generar_pdf, generar_excel

app = Flask(__name__)

# Secret key desde variable de entorno (más seguro para producción)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('RENDER') == 'true'  # HTTPS solo en producción
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 1800  # 30 minutos

# Agregar compresión GZIP para mejor rendimiento
Compress(app)

# Configuración para subir imágenes
UPLOAD_FOLDER = 'static/uploads/clubes'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Asegurarse de que la carpeta de subida exista
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ════════════════════════════════════════════════════════════════════
# POOL DE CONEXIONES - CONFIGURACIÓN OPTIMIZADA
# ════════════════════════════════════════════════════════════════════

db_pool = None

def init_connection_pool():
    """Inicializa el pool de conexiones a la base de datos"""
    global db_pool
    try:
        db_host = os.environ.get('DB_HOST', 'mysql-3f49e41c-axelpsorino03-a945.h.aivencloud.com')
        db_user = os.environ.get('DB_USER', 'avnadmin')
        db_password = os.environ.get('DB_PASSWORD', '')
        db_name = os.environ.get('DB_NAME', 'defaultdb')
        db_port = int(os.environ.get('DB_PORT', 18162))
        
        if not db_password:
            logging.error("⚠️ CRÍTICO: DB_PASSWORD no está configurada en variables de entorno")
            return False
        
        # REDUCIDO de 35 a 15 para mejor rendimiento en Render
        db_pool = pooling.MySQLConnectionPool(
            pool_name="clubgest_pool",
            pool_size=15,  # Optimizado para Render
            pool_reset_session=True,
            host=db_host,
            user=db_user,
            password=db_password,
            database=db_name,
            port=db_port,
            connection_timeout=30,
            autocommit=False,
            auth_plugin='mysql_native_password',
            ssl_verify_cert=True,
            ssl_verify_identity=True
        )
        logging.info("✅ Pool de conexiones inicializado (tamaño 15)")
        return True
    except Error as e:
        logging.error(f"❌ Error al inicializar pool: {e}")
        return False

def get_db_connection():
    """Obtiene una conexión del pool"""
    global db_pool
    if db_pool is None:
        if not init_connection_pool():
            return None
    try:
        return db_pool.get_connection()
    except Error as e:
        logging.error(f"❌ Error al obtener conexión: {e}")
        return None

@contextmanager
def get_db():
    """Context manager para usar conexión y cursor automáticamente"""
    conn = get_db_connection()
    if conn is None:
        raise Exception("No hay conexión a la base de datos")
    
    cursor = conn.cursor(dictionary=True)
    try:
        yield conn, cursor
    except Exception as e:
        logging.error(f"❌ Error en transacción: {e}")
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

# ════════════════════════════════════════════════════════════════════
# SEGURIDAD - LOGIN MEJORADO
# ════════════════════════════════════════════════════════════════════

# Configuración segura desde variables de entorno
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH', hashlib.sha256('1234'.encode()).hexdigest())

def verify_admin_password(password):
    """Verifica la contraseña usando hash SHA256"""
    return hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH

def login_required(f):
    """Decorador para proteger rutas de administrador"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "admin" not in session:
            flash("Por favor inicia sesión primero", "warning")
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated_function

# ════════════════════════════════════════════════════════════════════
# RUTAS PRINCIPALES
# ════════════════════════════════════════════════════════════════════

@app.route('/imagenes/<path:filename>')
def serve_image(filename):
    """Sirve las imágenes de la carpeta imagenes/"""
    return send_from_directory('imagenes', filename)

@app.route("/")
def inicio():
    return render_template("inicio.html")

@app.route("/formulario")
def formulario():
    """Muestra el formulario de registro con niveles y especialidades"""
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles")
            niveles = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades")
            especialidades = cursor.fetchall()
        return render_template("formulario.html", niveles=niveles, especialidades=especialidades)
    except Exception as e:
        logging.error(f"❌ Error en /formulario: {e}")
        return render_template("error.html", error="Error al cargar el formulario")

@app.route("/inscribirse", methods=["POST"])
def inscribirse():
    """Guarda al estudiante en la base de datos después de validar su correo"""
    nombre = request.form["nombre"]
    apellido = request.form["apellido"]
    correo = request.form["correo"].lower()
    genero = request.form["genero"]
    nivel = request.form["nivel"]
    especialidad = request.form["especialidad"]

    try:
        with get_db() as (conn, cursor):
            # Validación estricta de correo institucional
            if not correo.endswith("@donboscolatola.edu.ec"):
                cursor.execute("SELECT * FROM niveles")
                niveles = cursor.fetchall()
                cursor.execute("SELECT * FROM especialidades")
                especialidades = cursor.fetchall()
                return render_template("formulario.html", 
                                     error="Solo se permiten correos institucionales @donboscolatola.edu.ec",
                                     niveles=niveles,
                                     especialidades=especialidades)

            # Verificar si el correo ya existe
            cursor.execute("SELECT * FROM estudiantes WHERE correo_institucional=%s", (correo,))
            if cursor.fetchone():
                cursor.execute("SELECT * FROM niveles")
                niveles = cursor.fetchall()
                cursor.execute("SELECT * FROM especialidades")
                especialidades = cursor.fetchall()
                return render_template("formulario.html", 
                                     error="Este correo ya está registrado. Si no has elegido club, contacta al administrador.",
                                     niveles=niveles,
                                     especialidades=especialidades)

            # Insertar estudiante
            sql = "INSERT INTO estudiantes (nombres, apellidos, correo_institucional, genero, id_nivel, id_especialidad) VALUES (%s,%s,%s,%s,%s,%s)"
            cursor.execute(sql, (nombre, apellido, correo, genero, nivel, especialidad))
            conn.commit()
            
            # Guardar datos en sesión para el proceso de inscripción
            session["id_estudiante"] = cursor.lastrowid
            session["nivel"] = nivel

        return redirect("/clubes")
    except Exception as e:
        logging.error(f"❌ Error en /inscribirse: {e}")
        return render_template("error.html", error="Error al registrar estudiante")

@app.route("/cuestionario")
def cuestionario():
    """Muestra las preguntas para recomendar clubes (si el estudiante quiere ayuda)"""
    if "id_estudiante" not in session:
        return redirect("/formulario")
    return render_template("preguntas.html", preguntas=PREGUNTAS_RECOMENDACION)

@app.route("/guardar_respuestas", methods=["POST"])
def guardar_respuestas():
    """Guarda las respuestas del cuestionario y redirige a la recomendación"""
    if "id_estudiante" not in session:
        return redirect("/formulario")
    
    respuestas = []
    for i in range(len(PREGUNTAS_RECOMENDACION)):
        respuesta = request.form.get(f"pregunta_{i}")
        if respuesta:
            respuestas.append(respuesta)
    
    if len(respuestas) == len(PREGUNTAS_RECOMENDACION):
        session["respuestas_recomendacion"] = respuestas
        return redirect("/recomendacion_clubes")
    else:
        return "Por favor responde todas las preguntas para poder ayudarte.", 400

@app.route("/recomendacion_clubes")
def recomendacion_clubes():
    """Calcula el perfil del estudiante y muestra clubes recomendados"""
    if "id_estudiante" not in session:
        return redirect("/formulario")
    if "respuestas_recomendacion" not in session:
        return redirect("/cuestionario")
    
    try:
        with get_db() as (conn, cursor):
            nivel = int(session.get("nivel"))
            
            # Obtener clubes disponibles con sus cupos
            cursor.execute("""
                SELECT clubes.*, niveles.nombre_nivel,
                (SELECT COUNT(*) FROM inscripciones WHERE inscripciones.id_club = clubes.id_club) as inscritos,
                (clubes.cupo_maximo - (SELECT COUNT(*) FROM inscripciones WHERE inscripciones.id_club = clubes.id_club)) AS cupos_restantes
                FROM clubes JOIN niveles ON clubes.id_nivel = niveles.id_nivel
                WHERE clubes.id_nivel = %s AND clubes.activo = 1
            """, (nivel,))
            clubes_disponibles = cursor.fetchall()
            
            # Analizar respuestas y recomendar
            respuestas = session["respuestas_recomendacion"]
            recomendacion = calcular_recomendacion(respuestas)
            clubes_recomendados, otros_clubes = obtener_clubes_recomendados(clubes_disponibles, recomendacion["categoria"])
            
            return render_template("recomendacion.html", recomendacion=recomendacion, 
                                   clubes_recomendados=clubes_recomendados, otros_clubes=otros_clubes, 
                                   clubes_disponibles=clubes_disponibles)
    except Exception as e:
        logging.error(f"❌ Error en /recomendacion_clubes: {e}")
        return render_template("error.html", error="Error al cargar recomendaciones")

@app.route("/clubes")
def clubes():
    """Lista todos los clubes disponibles para el nivel del estudiante"""
    if "id_estudiante" not in session:
        return redirect("/formulario")

    try:
        with get_db() as (conn, cursor):
            nivel = int(session.get("nivel"))
            
            cursor.execute("""
            SELECT clubes.*, (SELECT COUNT(*) FROM inscripciones WHERE inscripciones.id_club = clubes.id_club) as inscritos,
            (clubes.cupo_maximo - (SELECT COUNT(*) FROM inscripciones WHERE inscripciones.id_club = clubes.id_club)) AS cupos_restantes
            FROM clubes WHERE clubes.id_nivel = %s AND clubes.activo = 1
            """, (nivel,))

            clubes_lista = cursor.fetchall()
            disponible = len(clubes_lista) > 0

        return render_template("clubes.html", clubes=clubes_lista, disponible=disponible)
    except Exception as e:
        logging.error(f"❌ Error en /clubes: {e}")
        return render_template("error.html", error="Error al cargar clubes")

@app.route("/inscribir_club", methods=["POST"])
def inscribir_club():
    """
    Endpoint mejorado para inscribir estudiantes en clubes con manejo de concurrencia.
    Soporta 50+ usuarios simultáneos sin race conditions.
    """
    
    # Validación inicial
    if "id_estudiante" not in session:
        logging.warning("❌ Intento de inscripción sin sesión")
        return redirect("/")

    estudiante = session.get("id_estudiante")
    club = request.form.get("club", "").strip()

    if not club:
        logging.warning(f"❌ Estudiante {estudiante} no seleccionó club")
        return render_template("error.html", error="Error: No seleccionaste ningún club.")

    # Validar que club sea número
    try:
        club_id = int(club)
    except (ValueError, TypeError):
        logging.warning(f"❌ Club inválido: {club}")
        return render_template("error.html", error="Error: Club no válido.")

    logging.info(f"📝 Inscripción iniciada - Estudiante: {estudiante}, Club: {club_id}")

    # Obtener conexión del pool
    conn = get_db_connection()
    if not conn:
        logging.error("❌ No hay conexión a BD")
        return render_template("error.html", error="Error de conexión a BD.")

    cursor = None
    max_reintentos = 3
    reintento = 0

    while reintento < max_reintentos:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # TRANSACCIÓN SERIALIZABLE (aísla completamente)
            conn.start_transaction(isolation_level='SERIALIZABLE', read_only=False)
            
            # Verificar que estudiante existe
            cursor.execute(
                "SELECT id_estudiante, id_nivel FROM estudiantes WHERE id_estudiante = %s",
                (estudiante,)
            )
            estudiante_data = cursor.fetchone()
            
            if not estudiante_data:
                conn.rollback()
                logging.warning(f"❌ Estudiante {estudiante} no existe")
                return render_template("error.html", error="Error: Tu registro no existe.")

            nivel_estudiante = estudiante_data['id_nivel']

            # Verificar que NO está ya inscrito
            cursor.execute(
                "SELECT id_inscripcion FROM inscripciones WHERE id_estudiante = %s",
                (estudiante,)
            )
            if cursor.fetchone():
                conn.rollback()
                logging.warning(f"❌ Estudiante {estudiante} ya inscrito")
                return render_template("error.html", error="Ya estás inscrito en otro club.")

            # ✅ CRÍTICO: SELECT FOR UPDATE bloquea la fila mientras la usamos
            cursor.execute(
                "SELECT id_club, id_nivel, cupo_maximo, nombre_club, activo FROM clubes WHERE id_club = %s FOR UPDATE",
                (club_id,)
            )
            club_data = cursor.fetchone()
            
            if not club_data:
                conn.rollback()
                logging.warning(f"❌ Club {club_id} no existe")
                return render_template("error.html", error="Error: El club no existe.")

            if not club_data['activo']:
                conn.rollback()
                return render_template("error.html", error="Este club está desactivado.")

            if club_data['id_nivel'] != nivel_estudiante:
                conn.rollback()
                return render_template("error.html", error="Club no disponible para tu nivel.")

            # Contar inscritos (dentro de la transacción bloqueada)
            cursor.execute(
                "SELECT COUNT(*) as total_inscritos FROM inscripciones WHERE id_club = %s",
                (club_id,)
            )
            count_result = cursor.fetchone()
            total_inscritos = count_result['total_inscritos'] if count_result else 0
            
            cupos_disponibles = club_data['cupo_maximo'] - total_inscritos
            
            logging.info(f"📊 Club {club_id}: Max={club_data['cupo_maximo']}, Inscritos={total_inscritos}, Disponibles={cupos_disponibles}")
            
            # VALIDACIÓN CRÍTICA: Cupos disponibles
            if cupos_disponibles <= 0:
                conn.rollback()
                logging.warning(f"❌ Club {club_id} lleno ({total_inscritos}/{club_data['cupo_maximo']})")
                return render_template("error.html", 
                    error=f"😔 {club_data['nombre_club']} está lleno.\n\nSolo se aceptaban {club_data['cupo_maximo']} estudiantes y se alcanzó el límite.")
            
            # Validación de valores negativos
            if club_data['cupo_maximo'] < 0 or total_inscritos < 0:
                conn.rollback()
                logging.error(f"❌ Valores de cupo inválidos")
                return render_template("error.html", error="Error interno en cálculo de cupos.")

            # INSERTAR INSCRIPCIÓN
            cursor.execute(
                "INSERT INTO inscripciones (id_estudiante, id_club, fecha_hora) VALUES (%s, %s, NOW())",
                (estudiante, club_id)
            )
            
            inscripcion_id = cursor.lastrowid
            logging.info(f"✅ Inscripción creada - ID: {inscripcion_id}, Estudiante: {estudiante}, Club: {club_id}")

            # COMMIT de la transacción
            conn.commit()
            logging.info(f"✅ Transacción confirmada")

            # Limpiar sesión
            session.pop("id_estudiante", None)
            session.pop("nivel", None)
            session.pop("respuestas_recomendacion", None)

            return render_template("exito.html")

        except mysql.connector.errors.DatabaseError as e:
            # Deadlock - reintentar
            if e.errno == 1213:
                reintento += 1
                logging.warning(f"⚠️ Deadlock. Reintentando ({reintento}/{max_reintentos})...")
                conn.rollback()
                time.sleep(0.1 * reintento)
                continue
            else:
                conn.rollback()
                logging.error(f"❌ Error BD: {e}")
                return render_template("error.html", error="Error en la BD.")
        
        except mysql.connector.errors.IntegrityError as e:
            conn.rollback()
            logging.error(f"❌ Integrity Error: {e}")
            if "UNIQUE" in str(e):
                return render_template("error.html", error="Ya estás inscrito en este club.")
            else:
                return render_template("error.html", error="Error al procesar tu inscripción.")
        
        except Exception as e:
            conn.rollback()
            logging.error(f"❌ Error inesperado: {e}")
            return render_template("error.html", error=f"Error: {str(e)}")
        
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    # Agotados reintentos
    logging.error(f"❌ Se agotaron reintentos")
    return render_template("error.html", error="El sistema está muy ocupado. Intenta de nuevo.")

@app.route("/api/cupos/<int:club_id>", methods=["GET"])
def api_cupos(club_id):
    """API endpoint para obtener cupos disponibles en TIEMPO REAL"""
    try:
        with get_db() as (conn, cursor):
            cursor.execute(
                """SELECT clubes.cupo_maximo, COUNT(inscripciones.id_inscripcion) as inscritos
                   FROM clubes 
                   LEFT JOIN inscripciones ON clubes.id_club = inscripciones.id_club
                   WHERE clubes.id_club = %s
                   GROUP BY clubes.id_club""",
                (club_id,)
            )
            
            resultado = cursor.fetchone()
            
            if not resultado:
                return jsonify({"error": "Club no encontrado"}), 404
            
            cupos_disponibles = max(0, resultado['cupo_maximo'] - resultado['inscritos'])
            
            return jsonify({
                "club_id": club_id,
                "cupo_maximo": resultado['cupo_maximo'],
                "inscritos": resultado['inscritos'],
                "cupos_disponibles": cupos_disponibles,
                "porcentaje_lleno": round((resultado['inscritos'] / resultado['cupo_maximo'] * 100), 1) if resultado['cupo_maximo'] > 0 else 0,
                "lleno": cupos_disponibles <= 0
            })
    except Exception as e:
        logging.error(f"❌ Error en API cupos: {e}")
        return jsonify({"error": str(e)}), 500

# ════════════════════════════════════════════════════════════════════
# ADMINISTRACIÓN - MEJORADA CON DECORADOR Y MANEJO DE ERRORES
# ════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    """Acceso para administradores con verificación segura"""
    if request.method == "POST":
        username = request.form.get("usuario", "")
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
    """Cierra la sesión actual"""
    session.clear()
    flash("Sesión cerrada correctamente", "info")
    return redirect("/")

@app.route("/admin")
@login_required
def admin():
    """Panel principal del administrador con estadísticas"""
    try:
        with get_db() as (conn, cursor):
            # Datos de clubes con cupos usados
            cursor.execute("""
            SELECT clubes.*, niveles.nombre_nivel, COUNT(inscripciones.id_inscripcion) AS cupos_usados
            FROM clubes LEFT JOIN inscripciones ON clubes.id_club = inscripciones.id_club
            JOIN niveles ON clubes.id_nivel = niveles.id_nivel GROUP BY clubes.id_club
            """)
            clubes = cursor.fetchall()

            cursor.execute("SELECT * FROM niveles")
            niveles = cursor.fetchall()

            cursor.execute("SELECT * FROM especialidades")
            especialidades = cursor.fetchall()

            # Últimas inscripciones para el panel
            cursor.execute("""
                SELECT e.nombres, e.apellidos, n.nombre_nivel, c.nombre_club,
                DATE(i.fecha_hora) as fecha, TIME(i.fecha_hora) as hora
                FROM inscripciones i JOIN estudiantes e ON i.id_estudiante = e.id_estudiante
                JOIN clubes c ON i.id_club = c.id_club JOIN niveles n ON e.id_nivel = n.id_nivel
                ORDER BY i.fecha_hora DESC LIMIT 15
            """)
            historial = cursor.fetchall()

        return render_template("admin.html", clubes=clubes, niveles=niveles, especialidades=especialidades, historial=historial)
    except Exception as e:
        logging.error(f"❌ Error en /admin: {e}")
        return render_template("error.html", error="Error al cargar panel administrativo")

@app.route("/crear_club", methods=["POST"])
@login_required
def crear_club():
    """Crea un nuevo club con su nombre, tutor, cupo y nivel"""
    nombre = request.form["nombre"]
    tutor = request.form.get("tutor", "Por asignar")
    descripcion = request.form.get("descripcion", "")
    cupo = int(request.form["cupo"])
    nivel = request.form["nivel"]

    try:
        with get_db() as (conn, cursor):
            # Verificar si el tutor ya existe
            if tutor != "Por asignar":
                cursor.execute("SELECT id_club FROM clubes WHERE tutor = %s", (tutor,))
                if cursor.fetchone():
                    flash(f"El tutor '{tutor}' ya ha sido asignado a otro club.", "error")
                    return redirect("/admin")
            
            # Manejo de la imagen
            filename = None
            if 'imagen' in request.files:
                file = request.files['imagen']
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    filename = f"{int(time.time())}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

            cursor.execute("INSERT INTO clubes (nombre_club, tutor, descripcion, imagen, cupo_maximo, id_nivel, activo) VALUES (%s, %s, %s, %s, %s, %s, 1)", 
                           (nombre, tutor, descripcion, filename, cupo, nivel))
            conn.commit()
            flash("Club creado exitosamente.", "success")
    except mysql.connector.Error as err:
        if err.errno == 1062:
            flash("Error: El tutor ya está asignado.", "error")
        else:
            flash(f"Error en la base de datos: {err}", "error")
    except Exception as e:
        logging.error(f"❌ Error en /crear_club: {e}")
        flash("Error al crear el club.", "error")
            
    return redirect("/admin")

@app.route("/editar_club/<id>", methods=["POST"])
@login_required
def editar_club(id):
    """Actualiza la información de un club, incluyendo su imagen si se sube una nueva"""
    nombre = request.form["nombre"]
    tutor = request.form.get("tutor", "Por asignar")
    descripcion = request.form.get("descripcion", "")
    cupo = int(request.form["cupo"])
    nivel = request.form["nivel"]
    eliminar_imagen = request.form.get("eliminar_imagen") == "1"

    try:
        with get_db() as (conn, cursor):
            # Verificar si el tutor ya existe en otro club
            if tutor != "Por asignar":
                cursor.execute("SELECT id_club FROM clubes WHERE tutor = %s AND id_club != %s", (tutor, id))
                if cursor.fetchone():
                    flash(f"El tutor '{tutor}' ya ha sido asignado a otro club.", "error")
                    return redirect("/admin")

            # Obtener la imagen actual del club
            cursor.execute("SELECT imagen FROM clubes WHERE id_club = %s", (id,))
            club_actual = cursor.fetchone()
            filename = club_actual['imagen'] if club_actual else None

            # Si se marcó para eliminar la imagen actual
            if eliminar_imagen and filename:
                try:
                    os.remove(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                except:
                    pass
                filename = None

            # Manejo de nueva imagen
            if 'imagen' in request.files:
                file = request.files['imagen']
                if file and allowed_file(file.filename):
                    if filename:
                        try:
                            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                        except:
                            pass
                    
                    filename = secure_filename(file.filename)
                    filename = f"{int(time.time())}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

            # Actualizar en la base de datos
            sql = """
                UPDATE clubes 
                SET nombre_club = %s, tutor = %s, descripcion = %s, imagen = %s, cupo_maximo = %s, id_nivel = %s 
                WHERE id_club = %s
            """
            cursor.execute(sql, (nombre, tutor, descripcion, filename, cupo, nivel, id))
            conn.commit()
            flash("Club actualizado exitosamente.", "success")
    except mysql.connector.Error as err:
        if err.errno == 1062:
            flash("Error: El tutor ya está asignado.", "error")
        else:
            flash(f"Error en la base de datos: {err}", "error")
    except Exception as e:
        logging.error(f"❌ Error en /editar_club: {e}")
        flash("Error al actualizar el club.", "error")
    
    return redirect("/admin")

@app.route("/desactivar/<id>")
@login_required
def desactivar(id):
    """Desactiva un club para que no reciba más inscripciones"""
    try:
        with get_db() as (conn, cursor):
            cursor.execute("UPDATE clubes SET activo = 0 WHERE id_club = %s", (id,))
            conn.commit()
            flash("Club desactivado exitosamente", "success")
    except Exception as e:
        logging.error(f"❌ Error en /desactivar: {e}")
        flash("Error al desactivar el club", "error")
    return redirect(request.headers.get("Referer") or "/admin")

@app.route("/activar/<id>")
@login_required
def activar(id):
    """Reactiva un club desactivado anteriormente"""
    try:
        with get_db() as (conn, cursor):
            cursor.execute("UPDATE clubes SET activo = 1 WHERE id_club = %s", (id,))
            conn.commit()
            flash("Club activado exitosamente", "success")
    except Exception as e:
        logging.error(f"❌ Error en /activar: {e}")
        flash("Error al activar el club", "error")
    return redirect(request.headers.get("Referer") or "/admin")

@app.route("/eliminar_club/<id>")
@login_required
def eliminar_club(id):
    """Elimina un club y libera a los estudiantes inscritos en él"""
    try:
        with get_db() as (conn, cursor):
            # Obtener estudiantes del club
            cursor.execute("SELECT id_estudiante FROM inscripciones WHERE id_club = %s", (id,))
            estudiantes = cursor.fetchall()
            
            # Eliminar inscripciones
            cursor.execute("DELETE FROM inscripciones WHERE id_club = %s", (id,))
            
            # Eliminar estudiantes para que puedan re-inscribirse con su correo
            if estudiantes:
                ids = [e['id_estudiante'] for e in estudiantes]
                placeholders = ','.join(['%s'] * len(ids))
                cursor.execute(f"DELETE FROM estudiantes WHERE id_estudiante IN ({placeholders})", tuple(ids))
            
            # Eliminar el club
            cursor.execute("DELETE FROM clubes WHERE id_club = %s", (id,))
            conn.commit()
            flash("Club eliminado exitosamente", "success")
    except Exception as e:
        logging.error(f"❌ Error en /eliminar_club: {e}")
        flash("Error al eliminar el club", "error")
    return redirect("/admin")

@app.route("/admin_inscripciones")
@login_required
def admin_inscripciones():
    """Reporte de estudiantes agrupados por nivel académico"""
    def get_lista(id_nivel):
        with get_db() as (conn, cursor):
            cursor.execute("""
            SELECT e.id_estudiante, e.nombres, e.apellidos, e.correo_institucional, e.genero, e.id_nivel, e.id_especialidad,
            c.id_club, c.nombre_club, c.tutor, esp.nombre_especialidad,
            DATE(i.fecha_hora) as fecha, TIME(i.fecha_hora) as hora
            FROM inscripciones i 
            JOIN estudiantes e ON i.id_estudiante = e.id_estudiante
            JOIN clubes c ON i.id_club = c.id_club
            JOIN especialidades esp ON e.id_especialidad = esp.id_especialidad
            WHERE e.id_nivel = %s
            ORDER BY e.apellidos, e.nombres
            """, (id_nivel,))
            return cursor.fetchall()

    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles")
            niveles_lista = cursor.fetchall()
            cursor.execute("SELECT * FROM clubes ORDER BY nombre_club")
            clubes_lista = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades ORDER BY nombre_especialidad")
            especialidades_lista = cursor.fetchall()

        return render_template("admin_inscripciones.html", 
                             primero=get_lista(1), 
                             segundo=get_lista(2), 
                             tercero=get_lista(3),
                             niveles_lista=niveles_lista,
                             clubes_lista=clubes_lista,
                             especialidades_lista=especialidades_lista)
    except Exception as e:
        logging.error(f"❌ Error en /admin_inscripciones: {e}")
        return render_template("error.html", error="Error al cargar el reporte")

@app.route("/admin_clubes")
@login_required
def admin_clubes():
    """Reporte de estudiantes agrupados por club"""
    def get_lista_club(id_nivel):
        with get_db() as (conn, cursor):
            cursor.execute("""
            SELECT clubes.id_club, clubes.nombre_club, clubes.tutor, estudiantes.id_estudiante,
            estudiantes.nombres, estudiantes.apellidos, estudiantes.correo_institucional,
            estudiantes.genero, estudiantes.id_nivel, estudiantes.id_especialidad, especialidades.nombre_especialidad,
            inscripciones.id_inscripcion, DATE(inscripciones.fecha_hora) as fecha, TIME(inscripciones.fecha_hora) as hora
            FROM clubes LEFT JOIN inscripciones ON clubes.id_club = inscripciones.id_club
            LEFT JOIN estudiantes ON inscripciones.id_estudiante = estudiantes.id_estudiante
            LEFT JOIN especialidades ON estudiantes.id_especialidad = especialidades.id_especialidad
            WHERE clubes.id_nivel = %s ORDER BY clubes.nombre_club, estudiantes.apellidos
            """, (id_nivel,))
            return cursor.fetchall()

    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles")
            niveles_lista = cursor.fetchall()
            cursor.execute("SELECT * FROM clubes ORDER BY nombre_club")
            clubes_lista = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades ORDER BY nombre_especialidad")
            especialidades_lista = cursor.fetchall()

        return render_template("admin_clubes.html", 
                             primero=get_lista_club(1), 
                             segundo=get_lista_club(2), 
                             tercero=get_lista_club(3), 
                             niveles_lista=niveles_lista, 
                             clubes_lista=clubes_lista,
                             especialidades_lista=especialidades_lista)
    except Exception as e:
        logging.error(f"❌ Error en /admin_clubes: {e}")
        return render_template("error.html", error="Error al cargar el reporte")

@app.route("/editar_estudiante", methods=["POST"])
@login_required
def editar_estudiante():
    """Edita todos los datos del estudiante y su inscripción a un club"""
    id_est = request.form.get("id_estudiante")
    nombres = request.form.get("nombres")
    apellidos = request.form.get("apellidos")
    correo = request.form.get("correo")
    genero = request.form.get("genero")
    id_niv = request.form.get("id_nivel")
    id_esp = request.form.get("id_especialidad")
    id_club = request.form.get("id_club")

    try:
        with get_db() as (conn, cursor):
            # Actualizar datos básicos en la tabla estudiantes
            sql_est = """
                UPDATE estudiantes 
                SET nombres = %s, apellidos = %s, correo_institucional = %s, genero = %s, id_nivel = %s, id_especialidad = %s 
                WHERE id_estudiante = %s
            """
            cursor.execute(sql_est, (nombres, apellidos, correo, genero, id_niv, id_esp, id_est))

            # Actualizar club en la tabla inscripciones
            sql_ins = "UPDATE inscripciones SET id_club = %s WHERE id_estudiante = %s"
            cursor.execute(sql_ins, (id_club, id_est))

            conn.commit()
            flash("Datos del estudiante actualizados correctamente.", "success")
    except Exception as e:
        logging.error(f"❌ Error al editar estudiante: {e}")
        flash(f"Error al actualizar: {str(e)}", "error")

    return redirect(request.referrer or "/admin_clubes")

@app.route("/admin_Informes")
@login_required
def admin_informes():
    """Panel de generación de informes"""
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM niveles")
            niveles = cursor.fetchall()
            cursor.execute("SELECT * FROM especialidades")
            especialidades = cursor.fetchall()
            cursor.execute("SELECT id_club, nombre_club FROM clubes ORDER BY nombre_club")
            clubes = cursor.fetchall()

        return render_template("admin_Informes.html", niveles=niveles, especialidades=especialidades, clubes=clubes)
    except Exception as e:
        logging.error(f"❌ Error en /admin_informes: {e}")
        return render_template("error.html", error="Error al cargar panel de informes")

# ════════════════════════════════════════════════════════════════════
# APIs - CON MANEJO DE ERRORES MEJORADO
# ════════════════════════════════════════════════════════════════════

@app.route("/buscar_estudiante")
def buscar_estudiante():
    """API para buscar estudiantes por nombre, apellido o correo"""
    try:
        query = request.args.get("q", "")
        if not query or len(query) < 2:
            return jsonify({"resultados": []})
        
        like_query = f"%{query}%"
        
        with get_db() as (conn, cursor):
            cursor.execute("""
            SELECT e.id_estudiante, e.nombres, e.apellidos, e.correo_institucional, e.genero, 
                   e.id_nivel, e.id_especialidad, c.id_club, c.nombre_club, n.nombre_nivel
            FROM estudiantes e 
            LEFT JOIN inscripciones i ON e.id_estudiante = i.id_estudiante
            LEFT JOIN clubes c ON i.id_club = c.id_club 
            LEFT JOIN niveles n ON e.id_nivel = n.id_nivel
            WHERE e.nombres LIKE %s OR e.apellidos LIKE %s OR e.correo_institucional LIKE %s 
            LIMIT 10
            """, (like_query, like_query, like_query))
            
            resultados = cursor.fetchall()
            
        return jsonify({"resultados": resultados})
    except Exception as e:
        logging.error(f"❌ Error en buscar_estudiante: {e}")
        return jsonify({"error": str(e), "resultados": []}), 500

@app.route("/verificar_correo")
def verificar_correo():
    """API para verificar si un correo ya está registrado"""
    try:
        correo = request.args.get("correo", "").lower().strip()
        if not correo:
            return jsonify({"registrado": False})
        
        with get_db() as (conn, cursor):
            cursor.execute("SELECT COUNT(*) as total FROM estudiantes WHERE correo_institucional = %s", (correo,))
            resultado = cursor.fetchone()
            
        return jsonify({"registrado": resultado["total"] > 0})
    except Exception as e:
        logging.error(f"❌ Error en verificar_correo: {e}")
        return jsonify({"error": str(e), "registrado": False}), 500

@app.route("/get_clubes_por_nivel")
def get_clubes_por_nivel():
    """API para obtener clubes filtrados por nivel para el reporte avanzado"""
    try:
        id_nivel = request.args.get("id_nivel", "todos")
        
        with get_db() as (conn, cursor):
            if id_nivel == "todos":
                cursor.execute("SELECT id_club, nombre_club FROM clubes ORDER BY nombre_club")
            else:
                cursor.execute("SELECT id_club, nombre_club FROM clubes WHERE id_nivel = %s ORDER BY nombre_club", (id_nivel,))
            
            clubes = cursor.fetchall()
            
        return jsonify({"clubes": clubes})
    except Exception as e:
        logging.error(f"❌ Error en get_clubes_por_nivel: {e}")
        return jsonify({"error": str(e), "clubes": []}), 500

# ════════════════════════════════════════════════════════════════════
# FUNCIONES DE REPORTES - CON USO CORRECTO DEL POOL
# ════════════════════════════════════════════════════════════════════

def obtener_datos_informe(tipo, filtro=None, filtros_avanzados=None):
    """
    Obtiene los datos de la base de datos según el tipo de informe y el filtro.
    tipo: 'nivel' | 'club' | 'especialidad' | 'avanzado'
    """
    sql = """
        SELECT e.nombres, e.apellidos, e.correo_institucional, e.genero,
               n.nombre_nivel, c.nombre_club, c.tutor, esp.nombre_especialidad
        FROM estudiantes e
        JOIN niveles n ON e.id_nivel = n.id_nivel
        JOIN inscripciones i ON e.id_estudiante = i.id_estudiante
        JOIN clubes c ON i.id_club = c.id_club
        JOIN especialidades esp ON e.id_especialidad = esp.id_especialidad
    """
    params = []
    where_clauses = []
    
    try:
        if tipo == 'avanzado' and filtros_avanzados:
            if filtros_avanzados.get('nivel') != 'todos':
                where_clauses.append("e.id_nivel = %s")
                params.append(filtros_avanzados['nivel'])
            if filtros_avanzados.get('club') != 'todos':
                where_clauses.append("c.id_club = %s")
                params.append(filtros_avanzados['club'])
            if filtros_avanzados.get('especialidad') != 'todos':
                where_clauses.append("e.id_especialidad = %s")
                params.append(filtros_avanzados['especialidad'])
        elif filtro and filtro != 'todos':
            if tipo == 'nivel':
                where_clauses.append("e.id_nivel = %s")
                params.append(filtro)
            elif tipo == 'club':
                where_clauses.append("c.id_club = %s")
                params.append(filtro)
            elif tipo == 'especialidad':
                where_clauses.append("e.id_especialidad = %s")
                params.append(filtro)

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        sql += " ORDER BY e.apellidos, e.nombres"
        
        with get_db() as (conn, cursor):
            cursor.execute(sql, tuple(params))
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"❌ Error en obtener_datos_informe: {e}")
        return []

@app.route('/informe/<formato>/<tipo>')
@login_required
def informe(formato, tipo):
    """Genera y descarga informes en PDF o Excel"""
    try:
        if tipo == 'avanzado':
            filtros = {
                'nivel': request.args.get('nivel', 'todos'),
                'club': request.args.get('club', 'todos'),
                'especialidad': request.args.get('especialidad', 'todos')
            }
            datos = obtener_datos_informe('avanzado', filtros_avanzados=filtros)
            
            # Construir título avanzado
            partes = []
            if filtros['nivel'] != 'todos':
                with get_db() as (conn, cursor):
                    cursor.execute("SELECT nombre_nivel FROM niveles WHERE id_nivel = %s", (filtros['nivel'],))
                    res = cursor.fetchone()
                    if res: partes.append(res['nombre_nivel'])
            if filtros['club'] != 'todos':
                with get_db() as (conn, cursor):
                    cursor.execute("SELECT nombre_club FROM clubes WHERE id_club = %s", (filtros['club'],))
                    res = cursor.fetchone()
                    if res: partes.append(res['nombre_club'])
            if filtros['especialidad'] != 'todos':
                with get_db() as (conn, cursor):
                    cursor.execute("SELECT nombre_especialidad FROM especialidades WHERE id_especialidad = %s", (filtros['especialidad'],))
                    res = cursor.fetchone()
                    if res: partes.append(res['nombre_especialidad'])
                
            filtro_nombre = " + ".join(partes) if partes else "Todos los registros"
            titulo = f"Reporte Personalizado: {filtro_nombre}"
        else:
            filtro = request.args.get('filtro', 'todos')
            datos = obtener_datos_informe(tipo, filtro)
            
            # Construir el título dinámico
            filtro_nombre = "Todos"
            if filtro != 'todos':
                if tipo == 'nivel':
                    with get_db() as (conn, cursor):
                        cursor.execute("SELECT nombre_nivel FROM niveles WHERE id_nivel = %s", (filtro,))
                        res = cursor.fetchone()
                        filtro_nombre = res['nombre_nivel'] if res else filtro
                elif tipo == 'club':
                    with get_db() as (conn, cursor):
                        cursor.execute("SELECT nombre_club FROM clubes WHERE id_club = %s", (filtro,))
                        res = cursor.fetchone()
                        filtro_nombre = res['nombre_club'] if res else filtro
                elif tipo == 'especialidad':
                    with get_db() as (conn, cursor):
                        cursor.execute("SELECT nombre_especialidad FROM especialidades WHERE id_especialidad = %s", (filtro,))
                        res = cursor.fetchone()
                        filtro_nombre = res['nombre_especialidad'] if res else filtro

            titulo = f"Reporte por {tipo.capitalize()}: {filtro_nombre}"
        
        if formato == 'pdf':
            report_type = tipo if tipo != 'avanzado' else 'nivel'
            return generar_pdf(datos, titulo, report_type)
        
        report_type = tipo if tipo != 'avanzado' else 'nivel'
        return generar_excel(datos, titulo, report_type)
    except Exception as e:
        logging.error(f"❌ Error en /informe: {e}")
        flash("Error al generar el informe", "error")
        return redirect("/admin_Informes")

# ════════════════════════════════════════════════════════════════════
# FUNCIONES UTILITARIAS
# ════════════════════════════════════════════════════════════════════

@app.context_processor
def utility_processor():
    """Función para asignar íconos automáticamente según el nombre del club"""
    def get_club_icon(nombre_club):
        n = nombre_club.lower()
        if any(x in n for x in ['compu', 'sist', 'prog', 'tec', 'robot']):
            return '💻'
        if any(x in n for x in ['fut', 'socc', 'depor', 'gym']):
            return '⚽'
        if any(x in n for x in ['mus', 'band', 'coro', 'guitar']):
            return '🎵'
        if any(x in n for x in ['art', 'pint', 'dibuj']):
            return '🎨'
        if any(x in n for x in ['cien', 'quim', 'biol']):
            return '🔬'
        if any(x in n for x in ['lect', 'libr', 'bibli']):
            return '📚'
        if any(x in n for x in ['teat', 'actua', 'cine']):
            return '🎭'
        return '✨'
    return dict(get_club_icon=get_club_icon)

@app.route('/error')
def error_page():
    return render_template("error.html", error="Ha ocurrido un error")

# ════════════════════════════════════════════════════════════════════
# INICIALIZACIÓN DE LA APLICACIÓN
# ════════════════════════════════════════════════════════════════════

# Inicializar el pool de conexiones al arrancar
init_connection_pool()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)