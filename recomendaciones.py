# Sistema de recomendación para ayudar a los estudiantes a elegir su club ideal

# Preguntas que el estudiante contesta para recibir recomendaciones personalizadas
PREGUNTAS_RECOMENDACION = [
    {
        "id": 1,
        "pregunta": "¿Qué tipo de actividades te llaman más la atención?",
        "opciones": [
            {"texto": "⚽ Deportes y actividad física", "pesos": {"Deportivo": 3, "Cultural": 0, "Tecnológico": 0, "Académico": 1}},
            {"texto": "🎨 Arte, música y cultura", "pesos": {"Deportivo": 0, "Cultural": 3, "Tecnológico": 0, "Académico": 1}},
            {"texto": "💻 Tecnología, robótica e innovación", "pesos": {"Deportivo": 0, "Cultural": 0, "Tecnológico": 3, "Académico": 2}},
            {"texto": "📚 Academias, debates y conocimiento", "pesos": {"Deportivo": 0, "Cultural": 1, "Tecnológico": 1, "Académico": 3}}
        ]
    },
    {
        "id": 2,
        "pregunta": "¿Cómo prefieres trabajar en equipo?",
        "opciones": [
            {"texto": "🏃‍♂️ Competir y superar retos", "pesos": {"Deportivo": 3, "Cultural": 1, "Tecnológico": 1, "Académico": 1}},
            {"texto": "🤝 Colaborar en proyectos creativos", "pesos": {"Deportivo": 1, "Cultural": 3, "Tecnológico": 2, "Académico": 2}},
            {"texto": "🔧 Resolver problemas técnicos", "pesos": {"Deportivo": 0, "Cultural": 0, "Tecnológico": 3, "Académico": 2}},
            {"texto": "🎤 Expresarme y liderar grupos", "pesos": {"Deportivo": 2, "Cultural": 3, "Tecnológico": 1, "Académico": 2}}
        ]
    },
    {
        "id": 3,
        "pregunta": "¿Qué habilidad quieres desarrollar más?",
        "opciones": [
            {"texto": "⚡ Agilidad y coordinación física", "pesos": {"Deportivo": 3, "Cultural": 0, "Tecnológico": 0, "Académico": 0}},
            {"texto": "🎭 Creatividad e imaginación", "pesos": {"Deportivo": 0, "Cultural": 3, "Tecnológico": 1, "Académico": 1}},
            {"texto": "🧠 Pensamiento lógico y programación", "pesos": {"Deportivo": 0, "Cultural": 0, "Tecnológico": 3, "Académico": 2}},
            {"texto": "🗣️ Comunicación y oratoria", "pesos": {"Deportivo": 1, "Cultural": 2, "Tecnológico": 0, "Académico": 3}}
        ]
    },
    {
        "id": 4,
        "pregunta": "¿Qué ambiente te motiva más?",
        "opciones": [
            {"texto": "🏟️ Espacios abiertos y energía", "pesos": {"Deportivo": 3, "Cultural": 1, "Tecnológico": 0, "Académico": 0}},
            {"texto": "🎪 Espacios creativos y dinámicos", "pesos": {"Deportivo": 1, "Cultural": 3, "Tecnológico": 1, "Académico": 1}},
            {"texto": "💡 Laboratorios y tecnología", "pesos": {"Deportivo": 0, "Cultural": 0, "Tecnológico": 3, "Académico": 2}},
            {"texto": "📖 Bibliotecas y estudio", "pesos": {"Deportivo": 0, "Cultural": 1, "Tecnológico": 1, "Académico": 3}}
        ]
    }
]

# Palabras clave para identificar qué club pertenece a cada categoría
CATEGORIAS_CLUBES = {
    "Deportivo": ["Fútbol", "Baloncesto", "Voleibol", "Natación", "Atletismo", "Deportivo", "Deportes", "Gym", "Gimnasio"],
    "Cultural": ["Música", "Teatro", "Danza", "Arte", "Cultura", "Literatura", "Coro", "Banda", "Pintura", "Dibujo"],
    "Tecnológico": ["Robótica", "Programación", "Tecnología", "Innovación", "STEM", "Videojuegos", "Informática", "Sistemas", "IA"],
    "Académico": ["Matemáticas", "Ciencias", "Debate", "Inglés", "Académico", "Investigación", "Lectura", "Ajedrez", "Física", "Química"]
}


def calcular_recomendacion(respuestas_usuario):
    """Calcula la categoría de club que mejor se adapta a las respuestas del estudiante"""
    puntuaciones = {"Deportivo": 0, "Cultural": 0, "Tecnológico": 0, "Académico": 0}
    
    for i, respuesta in enumerate(respuestas_usuario):
        pregunta = PREGUNTAS_RECOMENDACION[i]
        opcion = pregunta["opciones"][int(respuesta)]
        for categoria, peso in opcion["pesos"].items():
            puntuaciones[categoria] += peso
    
    categoria_recomendada = max(puntuaciones, key=puntuaciones.get)
    total = sum(puntuaciones.values())
    
    return {
        "categoria": categoria_recomendada,
        "puntuaciones": puntuaciones,
        "porcentajes": {cat: round((score / total * 100), 1) for cat, score in puntuaciones.items()}
    }


def obtener_clubes_recomendados(clubes_disponibles, categoria_recomendada):
    """Filtra los clubes que coinciden con la categoría recomendada"""
    palabras_clave = CATEGORIAS_CLUBES.get(categoria_recomendada, [])
    recomendados = []
    otros = []
    
    for club in clubes_disponibles:
        nombre_club = club.get('nombre_club', '').lower()
        es_recomendado = any(palabra.lower() in nombre_club for palabra in palabras_clave)
        
        if es_recomendado:
            recomendados.append(club)
        else:
            otros.append(club)
    
    # Si no hay coincidencias exactas, mostramos algunos clubes de todas formas
    if not recomendados and otros:
        recomendados = otros[:2]
        otros = otros[2:]
    
    return recomendados, otros