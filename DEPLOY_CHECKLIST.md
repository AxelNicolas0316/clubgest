# 🚀 CHECKLIST PRE-DEPLOY A RENDER - Mañana 50 estudiantes

## ✅ Cambios ya realizados en el código:

- ✅ Restaurados archivos críticos: `recomendaciones.py` y `app_informes.py`
- ✅ Agregado Flask-Compress para mejor rendimiento
- ✅ Pool de conexiones aumentado a 30 (para 50 usuarios)
- ✅ Secret key ahora variable de entorno (más seguro)
- ✅ Seguridad mejorada: HTTPS only cookies, HttpOnly, SameSite
- ✅ Gunicorn optimizado: 4 workers, 2 threads cada uno, timeout 120s
- ✅ Logging configurado para monitoreo en Render

## 🔴 **TAREAS CRÍTICAS A COMPLETAR EN RENDER ANTES DE MAÑANA:**

### 1. **Ir a Render Dashboard: https://dashboard.render.com**

### 2. **Seleccionar servicio: `clubgest`**

### 3. **Environment → Add Environment Variable**
   
   **Variable 1: DB_PASSWORD**
   - Key: `DB_PASSWORD`
   - Value: `[Copiar contraseña de Aiven sin comillas]`
   - ⚠️ **CRÍTICO**: Sin esto, la app NO se conectará a la BD

   **Variable 2: SECRET_KEY** 
   - Key: `SECRET_KEY`
   - Value: Ejecutar en terminal:
     ```
     python -c "import secrets; print(secrets.token_urlsafe(32))"
     ```
   - Copiar el resultado (ej: `HfJ8k_xP2mN-vQrSt9uW...`)

### 4. **Commit y push a GitHub**
   ```bash
   git add .
   git commit -m "Optimización para producción: 50 usuarios concurrentes"
   git push origin main
   ```

### 5. **Deploy en Render**
   - Ir a Render → `clubgest` → Manual Deploy → Deploy
   - Esperar a que compile e inicie

### 6. **Verificaciones finales** (después del deploy)
   - [ ] Acceder a https://clubgest.onrender.com
   - [ ] Login de admin
   - [ ] Crear un usuario de prueba
   - [ ] Registrarse en un club
   - [ ] Verificar en logs de Render que todo está correcto
   - [ ] Revisar Application Insights (si está configurado)

## 📊 Cambios de configuración:

| Parámetro | Antes | Ahora | Razón |
|-----------|-------|-------|-------|
| Plan | free | standard | Soportar 50 usuarios |
| Pool size | 20 | 30 + 5 overflow | 50 usuarios concurrentes |
| Workers | 1 | 4 | Paralelismo |
| Threads | - | 2/worker | 8 threads totales |
| Timeout | - | 120s | Queries largas |
| Compresión | No | Sí (GZIP) | Reducir ancho banda 30-50% |

## 🔍 Monitoreo recomendado después del deploy:

1. **Logs en Render** → Ver errores en tiempo real
2. **Performance** → Monitorear CPU y memoria
3. **Database** → Verificar conexiones activas en Aiven
4. **Errores 500** → Revisar cada 30 min durante primer uso

## ⚠️ Si algo falla:

1. **La app no inicia** → Revisar logs, probablemente falta DB_PASSWORD
2. **Errores de conexión** → Verificar variables de BD en Render
3. **Lentitud** → Revisar logs, puede ser pool agotado o queries largas
4. **Session errors** → Verificar SECRET_KEY fue agregada

## 🎯 Status final para mañana:

- ✅ Código optimizado: **HECHO**
- ⏳ Variables de entorno en Render: **FALTA - URGENTE**
- ⏳ Deploy: **FALTA - ANTES DE LA 1 PM**
- ⏳ Test final: **FALTA - 30 min antes de que lleguen alumnos**

---
**Última actualización:** 26 Abril 2026  
**Listos para:** 50 estudiantes simultáneos  
**Plan de contingencia:** Si falla, rollback inmediato a versión anterior en git
