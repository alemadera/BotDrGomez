from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler
)
import logging
import asyncio
import os
from datetime import datetime, timedelta, time
import pytz
from dateutil import parser as date_parser
from google.oauth2 import service_account
from googleapiclient.discovery import build
import gspread

# Configuración de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Estados de la conversación
(
    MENU_ES, POLICIES, CONFIRMATION,
    CITA_NOMBRE, CITA_NACIMIENTO, CITA_EDAD, CITA_DOCUMENTO, CITA_OCUPACION,
    CITA_REFERIDO, CITA_TELFIJO, CITA_CELULAR, CITA_CORREO, CITA_DIRECCION,
    TRATAMIENTO_MENU, TRATAMIENTO_INFO, PRECIOS_MENU, PRECIOS_DECISION, 
    EDUCACION_MENU, EDUCACION_DECISION, CONTACTO_OPCION, CONTACTO_RESPUESTA,
    SUERO_MENU, SUERO_BIENESTAR, SUERO_HORMONAL, SUERO_POSTQX, SUERO_INFO,
    CITAS_CONFIRMAR_PACIENTE, CITAS_ELEGIR_HORARIO
) = range(28)

# ======== CONFIG & CLIENTS GOOGLE ========
SHEETS_SPREADSHEET_ID = os.environ.get('GOOGLE_SHEETS_SPREADSHEET_ID')
SHEETS_PACIENTE_SHEET_NAME = os.environ.get('GOOGLE_SHEETS_PACIENTE', 'paciente')
SHEETS_AGENDA_SHEET_NAME = os.environ.get('GOOGLE_SHEETS_AGENDA', 'AgendaCitas')
CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'primary')
TIMEZONE = os.environ.get('TIMEZONE', 'America/Bogota')
BUSINESS_HOURS_START = os.environ.get('BUSINESS_HOURS_START', '08:00')
BUSINESS_HOURS_END = os.environ.get('BUSINESS_HOURS_END', '17:00')
SLOT_MINUTES = int(os.environ.get('SLOT_MINUTES', '30'))
BUSINESS_DAYS = os.environ.get('BUSINESS_DAYS', '1,2,3,4,5')  # 1=Lunes ... 7=Domingo
DAYS_AHEAD = int(os.environ.get('DAYS_AHEAD', '14'))

_GLOBAL_CREDENTIALS = None
_GSPREAD_CLIENT = None
_CALENDAR_SERVICE = None

GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/calendar'
]


def get_google_credentials():
    global _GLOBAL_CREDENTIALS
    if _GLOBAL_CREDENTIALS is not None:
        return _GLOBAL_CREDENTIALS

    creds = None
    json_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    json_inline = os.environ.get('GOOGLE_CREDENTIALS_JSON')

    if json_path and os.path.exists(json_path):
        creds = service_account.Credentials.from_service_account_file(json_path, scopes=GOOGLE_SCOPES)
    elif json_inline:
        import json
        info = json.loads(json_inline)
        creds = service_account.Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)
    else:
        logger.error("No se encontraron credenciales de Google. Configure GOOGLE_APPLICATION_CREDENTIALS o GOOGLE_CREDENTIALS_JSON.")
        raise RuntimeError("Credenciales de Google no configuradas")

    _GLOBAL_CREDENTIALS = creds
    return _GLOBAL_CREDENTIALS


def get_gspread_client():
    global _GSPREAD_CLIENT
    if _GSPREAD_CLIENT is None:
        creds = get_google_credentials()
        _GSPREAD_CLIENT = gspread.authorize(creds)
    return _GSPREAD_CLIENT


def get_calendar_service():
    global _CALENDAR_SERVICE
    if _CALENDAR_SERVICE is None:
        creds = get_google_credentials()
        _CALENDAR_SERVICE = build('calendar', 'v3', credentials=creds)
    return _CALENDAR_SERVICE


def sheets_find_patient(documento: str):
    if not SHEETS_SPREADSHEET_ID:
        logger.warning("GOOGLE_SHEETS_SPREADSHEET_ID no está configurado; omitiendo búsqueda en Sheets")
        return None
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SHEETS_SPREADSHEET_ID)
        ws = sh.worksheet(SHEETS_PACIENTE_SHEET_NAME)
        records = ws.get_all_records()
        for record in records:
            # Matcheo flexible por string
            if str(record.get('Documento', '')).strip() == str(documento).strip():
                return record
        return None
    except Exception as e:
        logger.exception(f"Error buscando paciente en Sheets: {e}")
        return None


def sheets_append_agenda(row_dict: dict):
    if not SHEETS_SPREADSHEET_ID:
        logger.warning("GOOGLE_SHEETS_SPREADSHEET_ID no está configurado; no se registrará la agenda en Sheets")
        return False
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SHEETS_SPREADSHEET_ID)
        ws = sh.worksheet(SHEETS_AGENDA_SHEET_NAME)
        # Orden de columnas propuesto
        valores = [
            row_dict.get('Fecha', ''),
            row_dict.get('HoraInicio', ''),
            row_dict.get('HoraFin', ''),
            row_dict.get('Documento', ''),
            row_dict.get('Nombre', ''),
            row_dict.get('CalendarEventId', ''),
            row_dict.get('Estado', ''),
            row_dict.get('CreadoEn', ''),
        ]
        ws.append_row(valores)
        return True
    except Exception as e:
        logger.exception(f"Error registrando agenda en Sheets: {e}")
        return False


def _parse_hhmm(value: str) -> time:
    parts = value.split(':')
    return time(hour=int(parts[0]), minute=int(parts[1]))


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def calendar_list_free_slots(limit: int = 8):
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    start_window = now
    end_window = now + timedelta(days=DAYS_AHEAD)

    service = get_calendar_service()

    # Obtener eventos existentes en ventana
    events = []
    try:
        page_token = None
        while True:
            resp = service.events().list(
                calendarId=CALENDAR_ID,
                timeMin=start_window.isoformat(),
                timeMax=end_window.isoformat(),
                singleEvents=True,
                orderBy='startTime',
                pageToken=page_token
            ).execute()
            events.extend(resp.get('items', []))
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
    except Exception as e:
        logger.exception(f"Error listando eventos del calendario: {e}")
        return []

    busy = []
    for ev in events:
        try:
            s = ev['start'].get('dateTime') or ev['start'].get('date')
            e = ev['end'].get('dateTime') or ev['end'].get('date')
            # Normalizar a datetime con tz
            if 'T' in s:
                s_dt = date_parser.isoparse(s)
                if s_dt.tzinfo is None:
                    s_dt = tz.localize(s_dt)
            else:
                # Evento de día completo; ocupar todo ese día
                s_dt = tz.localize(datetime.combine(date_parser.isoparse(s).date(), time(0, 0)))
            if 'T' in e:
                e_dt = date_parser.isoparse(e)
                if e_dt.tzinfo is None:
                    e_dt = tz.localize(e_dt)
            else:
                e_dt = tz.localize(datetime.combine(date_parser.isoparse(e).date(), time(23, 59)))
            busy.append((s_dt.astimezone(tz), e_dt.astimezone(tz)))
        except Exception:
            continue

    # Generar slots
    slots = []
    start_hhmm = _parse_hhmm(BUSINESS_HOURS_START)
    end_hhmm = _parse_hhmm(BUSINESS_HOURS_END)
    allowed_days = set(int(x.strip()) for x in BUSINESS_DAYS.split(',') if x.strip())

    cursor_day = start_window.date()
    while cursor_day <= end_window.date() and len(slots) < limit:
        day_dt = tz.localize(datetime.combine(cursor_day, time(0, 0)))
        weekday_1_7 = day_dt.isoweekday()  # 1=Lunes ... 7=Domingo
        if weekday_1_7 in allowed_days:
            day_start = tz.localize(datetime.combine(cursor_day, start_hhmm))
            day_end = tz.localize(datetime.combine(cursor_day, end_hhmm))
            cursor = max(day_start, start_window)
            while cursor + timedelta(minutes=SLOT_MINUTES) <= day_end and len(slots) < limit:
                slot_start = cursor
                slot_end = cursor + timedelta(minutes=SLOT_MINUTES)
                # Chequear choque con busy
                conflict = any(_overlaps(slot_start, slot_end, b0, b1) for b0, b1 in busy)
                if not conflict:
                    label = f"{slot_start.strftime('%Y-%m-%d %H:%M')} - {slot_end.strftime('%H:%M')}"
                    slots.append({
                        'label': label,
                        'start': slot_start.isoformat(),
                        'end': slot_end.isoformat()
                    })
                cursor += timedelta(minutes=SLOT_MINUTES)
        cursor_day = cursor_day + timedelta(days=1)

    return slots


def calendar_create_event(start_iso: str, end_iso: str, summary: str, description: str) -> str:
    service = get_calendar_service()
    event = {
        'summary': summary,
        'description': description,
        'start': {'dateTime': start_iso, 'timeZone': TIMEZONE},
        'end': {'dateTime': end_iso, 'timeZone': TIMEZONE},
    }
    created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    return created.get('id', '')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicio - Selección de idioma"""
    user = update.message.from_user
    logger.info(f"Usuario {user.first_name} inició el bot")
    
    buttons = [["🗓️ Agendar cita", "💊 Tratamientos"],
            ["📄 Enviar exámenes", "💧 Sueroterapia"],
            ["💰 Precios", "🌿 Medicina funcional"],
            ["👥 Contactar Asesor"]]
    await update.message.reply_text(
    "👋 ¡Hola! Soy el asistente virtual del Dr. Luis Fernando Gómez.\n\n"
    "Estoy aquí para ayudarte. Selecciona una opción del menú 👇",
    reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )
    return MENU_ES

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menú principal"""
    
    buttons = [["🗓️ Agendar cita", "💊 Tratamientos"],
                ["📄 Enviar exámenes", "💧 Sueroterapia"],
                ["💰 Precios", "🌿 Medicina funcional"],
                ["👥 Contactar Asesor"]]
    await update.message.reply_text(
        "🔄 Menú reiniciado. ¿En qué puedo ayudarte hoy?",
        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )
    return MENU_ES


# ======== MANEJADORES DE MENÚ EN ESPAÑOL ============
async def menu_es(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text

    if text == "🗓️ Agendar cita":
        await update.message.reply_text(
            "📋 *DATOS BÁSICOS DEL PACIENTE*\n\n"
            "A continuación, te pediremos la siguiente información:\n\n"
            "1️⃣ Nombre completo del paciente\n"
            "2️⃣ Fecha de nacimiento\n"
            "3️⃣ Años cumplidos\n"
            "4️⃣ Número de documento\n"
            "5️⃣ Ocupación\n"
            "6️⃣ Referido por\n"
            "7️⃣ Teléfono fijo\n"
            "8️⃣ Celular\n"
            "9️⃣ Correo electrónico\n"
            "🔟 Dirección de residencia\n\n"
            "📝 *Empecemos*. Por favor escribe el *nombre completo del paciente*:",
            parse_mode="Markdown"
        )
        return CITA_NOMBRE

    elif text == "💊 Tratamientos":
        return await tratamientos_menu(update, context)

    elif text == "📄 Enviar exámenes":
        await update.message.reply_text(
            "📎 Puedes enviar tus exámenes o información médica a:\n"
            "- Correo: doctor@correo.com\n"
            "- WhatsApp: +57 123 456 7890\n\n"
            "Incluye tu nombre completo y la fecha de tu cita.\n"
            "Si lo prefieres, también puedes adjuntar aquí el archivo."
        )
        return await handle_policies(update, context)

    elif text == "💧 Sueroterapia":
        return await suero_menu(update, context)

    elif text == "💰 Precios":
        return await precios_menu(update, context)
    
    elif text == "🌿 Medicina funcional":
        return await educacion_menu(update, context)
    
    elif text == "👥 Contactar Asesor":
        return await contacto_menu(update, context)

    else:
        await update.message.reply_text("Por favor elige una opción válida del menú.")
        return MENU_ES


async def tratamientos_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menú de opciones sobre tratamientos"""
    opciones = [["Condiciones homeopáticas", "Condiciones funcionales"],
                ["Tratamientos específicos", "¿Cómo funciona?"],
                ["Volver"]]

    await update.message.reply_text(
        "¿Sobre qué aspecto de los tratamientos deseas saber más?",
        reply_markup=ReplyKeyboardMarkup(opciones, resize_keyboard=True)
    )
    return TRATAMIENTO_MENU

async def tratamientos_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    respuestas = {
        "Condiciones homeopáticas": "📌 Tratamos alergias, ansiedad, insomnio, migraña, problemas digestivos, entre otros.",
        "Condiciones funcionales": "📌 Abordamos problemas como fatiga crónica, inflamación intestinal, desequilibrios hormonales, resistencia a la insulina, etc.",
        "Tratamientos específicos": "💊 Usamos fórmulas naturales, homeopatía, sueros funcionales, suplementos y dieta personalizada.",
        "¿Cómo funciona?": "⚙️ Combinamos diagnóstico funcional, medicina natural y tratamiento integral del origen del problema.",
        "Volver": "Regresando al menú principal..."
    }

    if text == "Volver":
        return await menu(update, context)

    if text in respuestas:
        await update.message.reply_text(respuestas[text])
        botones = [["Sí", "No"]]
        await update.message.reply_text(
            "¿Deseas saber algo más sobre los tratamientos?",
            reply_markup=ReplyKeyboardMarkup(botones, one_time_keyboard=True, resize_keyboard=True)
        )
        return TRATAMIENTO_INFO
    else:
        await update.message.reply_text("Selecciona una opción válida del menú de tratamientos.")
        return TRATAMIENTO_MENU

async def tratamientos_continuar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()

    if text == "sí":
        return await tratamientos_menu(update, context)
    elif text == "no":
        return await handle_policies(update, context)
    else:
        await update.message.reply_text("🔁 No entendí tu respuesta. Volveremos al menú principal.")
        return await menu(update, context)


async def precios_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    botones = [
        ["Primera consulta", "Seguimientos"],
        ["Paquetes funcionales", "Formas de pago"],
        ["Enlace de pago", "Volver"]
    ]
    await update.message.reply_text(
        "💰 ¿Qué información deseas consultar sobre precios?",
        reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True)
    )
    return PRECIOS_MENU

async def precios_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text

    respuestas = {
        "Primera consulta": "🩺 Valor: $150.000 COP\nDuración aproximada: 60 minutos.",
        "Seguimientos": "📅 Valor: $100.000 COP por sesión de control o evolución.",
        "Paquetes funcionales": "🎯 Tenemos paquetes mensuales desde $350.000 que incluyen consulta + tratamiento personalizado.",
        "Formas de pago": "💳 Aceptamos Nequi, Daviplata, transferencia bancaria y tarjeta.",
        "Enlace de pago": "🔗 Puedes pagar aquí: [https://tu-enlace-de-pago.com]",
    }

    if text in respuestas:
        await update.message.reply_text(respuestas[text])
        botones = [["Agendar cita", "Otra consulta", "No, gracias"]]
        await update.message.reply_text(
            "¿Deseas agendar una cita o consultar otra cosa?",
            reply_markup=ReplyKeyboardMarkup(botones, one_time_keyboard=True, resize_keyboard=True)
        )
        return PRECIOS_DECISION

    elif text == "Volver":
        return await menu(update, context)
    
    else:
        await update.message.reply_text("Selecciona una opción válida sobre precios.")
        return PRECIOS_MENU

async def precios_siguiente(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text

    if text == "Agendar cita":
        await update.message.reply_text("📝 ¿Cuál es tu *nombre completo* y *fecha de nacimiento*?", parse_mode="Markdown")
        return CITA_NOMBRE

    elif text == "Otra consulta":
        return await precios_menu(update, context)

    elif text == "No, gracias":
        return await handle_policies(update, context)

    else:
        await update.message.reply_text(
            "🔁 No entendí tu respuesta. Por favor selecciona una opción válida:",
            reply_markup=ReplyKeyboardMarkup(
                [["Agendar cita", "Otra consulta", "No, gracias"]],
                one_time_keyboard=True,
                resize_keyboard=True
            )
        )
        return PRECIOS_DECISION

async def educacion_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    botones = [
        ["¿Qué es medicina funcional?", "¿Qué es homeopatía?"],
        ["¿Cómo te ayuda?", "Testimonios"],
        ["Recomendaciones", "Volver"]
    ]
    await update.message.reply_text(
        "🌿 ¿Qué deseas saber sobre nuestro enfoque natural y funcional?",
        reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True)
    )
    return EDUCACION_MENU

async def educacion_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    # Verificar si el usuario quiere volver al menú principal
    if text == "Volver":
        return await menu(update, context)

    respuestas = {
        "¿Qué es medicina funcional?": "🧠 Es un enfoque que busca el origen de la enfermedad, no solo tratar síntomas.",
        "¿Qué es homeopatía?": "💧 Medicina natural basada en microdosis que estimulan la autocuración del cuerpo.",
        "¿Cómo te ayuda?": "✨ Ayuda a mejorar energía, digestión, inmunidad, hormonas y más, de forma personalizada.",
        "Testimonios": "📣 Hemos ayudado a cientos de pacientes a recuperar su bienestar.",
        "Recomendaciones": "📋 Llega a tu cita en ayunas (si aplica), con exámenes recientes y sin maquillaje si es facial.",
    }

    if text in respuestas:
        await update.message.reply_text(respuestas[text])
        botones = [["Sí", "No", "Volver"]]
        await update.message.reply_text(
            "¿Te gustaría agendar una cita o seguir consultando?",
            reply_markup=ReplyKeyboardMarkup(botones, one_time_keyboard=True, resize_keyboard=True)
        )
        return EDUCACION_DECISION

    await update.message.reply_text("Por favor elige una opción válida.")
    return EDUCACION_MENU


async def educacion_siguiente(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()

    if "sí" in text or "si" in text:
        await update.message.reply_text("📝 ¿Cuál es tu *nombre completo* y *fecha de nacimiento*?", parse_mode="Markdown")
        return CITA_NOMBRE
    elif "volver" in text:
        return await educacion_menu(update, context)
    else:
        return await handle_policies(update, context)

async def contacto_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    botones = [["Por WhatsApp", "Por llamada"], ["Volver al menú"]]
    await update.message.reply_text(
        "👥 ¿Cómo prefieres que alguien del equipo te contacte directamente?",
        reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True)
    )
    return CONTACTO_OPCION

async def contacto_respuesta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()

    if "whatsapp" in text:
        await update.message.reply_text(
            "📱 Nuestro equipo te contactará por WhatsApp al número que tenemos registrado.\n"
            "⏰ Horario de atención: Lunes a Viernes, 8am a 5pm."
        )
        await update.message.reply_text("⌛ Tiempo estimado de respuesta: dentro de las próximas 2 horas hábiles.")
        return await handle_policies(update, context)

    elif "llamada" in text or "llamar" in text:
        await update.message.reply_text(
            "📞 Nuestro equipo te llamará durante el horario de atención registrado.\n"
            "⏰ Lunes a Viernes, 8am a 5pm."
        )
        await update.message.reply_text("⌛ Tiempo estimado de respuesta: dentro de las próximas 2 horas hábiles.")
        return await handle_policies(update, context)

    elif "volver" in text or "menú" in text:
        return await menu(update, context)

    else:
        await update.message.reply_text(
            "❗ No entendí tu respuesta.\n\n"
            "Por favor elige una opción:",
            reply_markup=ReplyKeyboardMarkup(
                [["Por WhatsApp", "Por llamada"], ["Volver al menú"]],
                resize_keyboard=True
            )
        )
        return CONTACTO_OPCION

async def handle_policies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recordatorio de políticas antes de finalizar"""
    botones = [["Sí, estoy de acuerdo", "No"]]
    await update.message.reply_text(
        "📝 *Recordatorio de políticas:*\n\n"
        "- Las cancelaciones deben hacerse con al menos 24h de anticipación.\n"
        "- Todos los datos se tratan bajo confidencialidad.\n"
        "- Al continuar, aceptas el consentimiento informado.\n\n"
        "¿Estás de acuerdo?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(botones, one_time_keyboard=True, resize_keyboard=True)
    )
    return POLICIES

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirmación y cierre del flujo"""

    respuesta = update.message.text.lower().strip()

    if "sí" in respuesta or "si" in respuesta:
        await update.message.reply_text(
            "✅ ¡Gracias por contactarnos! Tu solicitud ha sido procesada.\n\n"
            "📎 Si deseas unirte a nuestra comunidad de WhatsApp: [Enlace aquí]\n"
            "📝 También puedes responder una encuesta rápida: [Enlace a encuesta]",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await update.message.reply_text(
            "Entendido. Si necesitas más información, puedes hablar con nuestro equipo.",
            reply_markup=ReplyKeyboardRemove()
        )

    # 👇 Pausa de seguridad para evitar que Telegram omita el segundo mensaje
    await asyncio.sleep(0.6)

    # 👇 Mensaje final con opciones claras
    await update.message.reply_text(
        "🔄 ¿Qué deseas hacer ahora?\n\n"
        "👉 *Volver al menú:* /menu\n"
        "🚪 *Cerrar la conversación:* /cancel",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["/menu", "/cancel"]], resize_keyboard=True)
    )

    return ConversationHandler.END

async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostrar menú principal sen español"""
    buttons = [["🗓️ Agendar cita", "💊 Tratamientos"],
                ["📄 Enviar exámenes", "💧 Sueroterapia"],
                ["💰 Precios", "🌿 Medicina funcional"],
                ["👥 Contactar Asesor"]]
    await update.message.reply_text(
        "🔄 Menú reiniciado. ¿En qué puedo ayudarte hoy?",
        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )
    return MENU_ES

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🚪 Sesión cancelada. Puedes volver a comenzar con /start o /menu.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def cita_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['nombre'] = update.message.text
    await update.message.reply_text("📅 Fecha de nacimiento (dd/mm/aaaa):")
    return CITA_NACIMIENTO

async def cita_nacimiento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['nacimiento'] = update.message.text
    await update.message.reply_text("🎂 ¿Cuántos años cumplidos tiene?")
    return CITA_EDAD

async def cita_edad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['edad'] = update.message.text
    await update.message.reply_text("🪪 Número de documento:")
    return CITA_DOCUMENTO

async def cita_documento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['documento'] = update.message.text

    # Intentar buscar en Google Sheets
    paciente = sheets_find_patient(context.user_data['documento'])
    if paciente:
        context.user_data['paciente_sheet'] = paciente
        resumen = (
            "🔎 Se encontró un registro del paciente:\n\n"
            f"🪪 Documento: {paciente.get('Documento','')}\n"
            f"👤 Nombre: {paciente.get('Nombre','')}\n"
            f"📅 Fecha Nacimiento: {paciente.get('Fecha Nacimiento','')}\n"
            f"💼 Ocupación: {paciente.get('Ocupación','')}\n"
            f"📞 Tel Fijo: {paciente.get('TelFIjo','')}\n"
            f"📱 Celular: {paciente.get('Celular','')}\n"
            f"📧 Correo: {paciente.get('Correo','')}\n"
            f"🏠 Dirección: {paciente.get('Dirección','')}\n"
            f"🗓️ Última cita: {paciente.get('Ultima cita','')}\n"
            f"📝 Motivo última consulta: {paciente.get('Motivo ultima consulta','')}\n\n"
            "¿Los datos son correctos para proceder a agendar?"
        )
        botones = [["Sí, agendar"], ["No, corregir datos"]]
        await update.message.reply_text(resumen, reply_markup=ReplyKeyboardMarkup(botones, one_time_keyboard=True, resize_keyboard=True))
        return CITAS_CONFIRMAR_PACIENTE

    await update.message.reply_text("💼 Ocupación:")
    return CITA_OCUPACION

async def cita_ocupacion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['ocupacion'] = update.message.text
    await update.message.reply_text("👤 Referido por:")
    return CITA_REFERIDO

async def cita_referido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['referido'] = update.message.text
    await update.message.reply_text("📞 Teléfono fijo:")
    return CITA_TELFIJO

async def cita_telefono_fijo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['telefono_fijo'] = update.message.text
    await update.message.reply_text("📱 Celular:")
    return CITA_CELULAR

async def cita_celular(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['celular'] = update.message.text
    await update.message.reply_text("📧 Correo electrónico:")
    return CITA_CORREO

async def cita_correo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['correo'] = update.message.text
    await update.message.reply_text("🏠 Dirección de residencia:")
    return CITA_DIRECCION

async def cita_direccion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['direccion'] = update.message.text

    # Resumen
    resumen = (
        "✅ *Datos recibidos:*\n\n"
        f"👤 Nombre: {context.user_data['nombre']}\n"
        f"📅 Nacimiento: {context.user_data['nacimiento']}\n"
        f"🎂 Edad: {context.user_data['edad']}\n"
        f"🪪 Documento: {context.user_data['documento']}\n"
        f"💼 Ocupación: {context.user_data['ocupacion']}\n"
        f"👤 Referido por: {context.user_data['referido']}\n"
        f"📞 Tel. fijo: {context.user_data['telefono_fijo']}\n"
        f"📱 Celular: {context.user_data['celular']}\n"
        f"📧 Correo: {context.user_data['correo']}\n"
        f"🏠 Dirección: {context.user_data['direccion']}\n\n"
        "Ahora, elige un horario disponible para tu cita."
    )

    await update.message.reply_text(resumen, parse_mode="Markdown")

    # Mostrar horarios disponibles
    slots = calendar_list_free_slots(limit=8)
    if not slots:
        await update.message.reply_text("No hay horarios disponibles en este momento. Intenta más tarde.")
        return await handle_policies(update, context)

    context.user_data['slots'] = slots
    botones = [[s['label']] for s in slots]
    await update.message.reply_text(
        "Selecciona un horario:",
        reply_markup=ReplyKeyboardMarkup(botones, one_time_keyboard=True, resize_keyboard=True)
    )
    return CITAS_ELEGIR_HORARIO

# ====== FLUJO DE SUEROTERAPIA ======
async def suero_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    botones = [["🧘‍♀️ Bienestar general", "🧬 Hormonales y metabólicos"],
            ["🩺 Postquirúrgicos y recuperación", "🔙 Volver al menú"]]
    await update.message.reply_text(
        "💧 Elige la categoría de suero que te interesa:",
        reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True)
    )
    return SUERO_MENU

async def suero_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    opcion = update.message.text

    if opcion == "🧘‍♀️ Bienestar general":
        botones = [["✨ Suero multivitamínico", "🍃 Detox hepático"],
                ["🕰️ Antiaging", "😌 Antiestrés"],
                ["🛡️ Inmunológico", "🔙 Volver"]]
        await update.message.reply_text("🧘‍♀️ Sueros para bienestar general:", reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True))
        return SUERO_BIENESTAR

    elif opcion == "🧬 Hormonales y metabólicos":
        botones = [["🔥 Metabolismo activo", "🧘‍♂️ Equilibrio hormonal"],
                ["🌸 Salud femenina", "🔙 Volver"]]
        await update.message.reply_text("🧬 Sueros hormonales y metabólicos:", reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True))
        return SUERO_HORMONAL

    elif opcion == "🩺 Postquirúrgicos y recuperación":
        botones = [["💪 Recuperación muscular", "🩹 Cicatrización avanzada"],
                ["🔙 Volver"]]
        await update.message.reply_text("🩺 Sueros para recuperación postquirúrgica:", reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True))
        return SUERO_POSTQX

    elif opcion == "🔙 Volver al menú":
        return await menu(update, context)
    
    else:
        await update.message.reply_text("Selecciona una opción válida.")
        return SUERO_MENU

async def suero_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    suero = update.message.text
    descripciones = {
        "✨ Suero multivitamínico": "💉 Combinación de vitaminas esenciales para energía, piel, sistema inmune y vitalidad general.",
        "🍃 Detox hepático": "🧪 Suero con antioxidantes que ayudan a eliminar toxinas y mejorar la función hepática.",
        "🕰️ Antiaging": "🕊️ Mezcla con efecto antioxidante y celular, ayuda a combatir el envejecimiento prematuro.",
        "😌 Antiestrés": "🌿 Ayuda a disminuir ansiedad, fatiga y estrés crónico, mejorando el estado de ánimo.",
        "🛡️ Inmunológico": "🛡️ Refuerza las defensas naturales del cuerpo y mejora la resistencia ante infecciones.",
        "🔥 Metabolismo activo": "⚡ Favorece la quema de grasa, energía celular y metabolismo basal.",
        "🧘‍♂️ Equilibrio hormonal": "🔄 Regula de forma natural niveles hormonales relacionados con fatiga, insomnio o estrés.",
        "🌸 Salud femenina": "👩 Suero enfocado en el bienestar hormonal, emocional y físico femenino.",
        "💪 Recuperación muscular": "💪 Ideal postejercicio o postcirugía, ayuda a reducir fatiga y dolores musculares.",
        "🩹 Cicatrización avanzada": "🧬 Estimula la regeneración tisular y mejora el proceso de cicatrización postoperatoria."
    }

    if suero in descripciones:
        await update.message.reply_text(descripciones[suero])
        botones = [["📅 Agendar cita", "💧 Ver otro suero", "🔙 Menú principal"]]
        await update.message.reply_text(
            "¿Qué deseas hacer ahora?",
            reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True)
        )
        return SUERO_INFO

    elif suero == "🔙 Volver":
        return await suero_menu(update, context)

    elif suero == "💧 Ver otro suero":
        return await suero_menu(update, context)

    elif suero == "📅 Agendar cita":
        await update.message.reply_text("📝 ¿Cuál es tu *nombre completo* y *fecha de nacimiento*?", parse_mode="Markdown")
        return CITA_NOMBRE

    elif suero == "🔙 Menú principal":
        return await menu(update, context)

    else:
        await update.message.reply_text("Por favor selecciona una opción válida.")
        return SUERO_INFO

# ====== NUEVOS HANDLERS DE AGENDA ======
async def confirmar_paciente(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto = update.message.text.strip().lower()
    if 'sí' in texto or 'si' in texto or 'agendar' in texto:
        # Mostrar horarios disponibles
        slots = calendar_list_free_slots(limit=8)
        if not slots:
            await update.message.reply_text("No hay horarios disponibles en este momento. Intenta más tarde.")
            return await handle_policies(update, context)
        context.user_data['slots'] = slots
        botones = [[s['label']] for s in slots]
        await update.message.reply_text(
            "Selecciona un horario:",
            reply_markup=ReplyKeyboardMarkup(botones, one_time_keyboard=True, resize_keyboard=True)
        )
        return CITAS_ELEGIR_HORARIO
    else:
        await update.message.reply_text("Entendido. Actualicemos tus datos. Por favor escribe el nombre completo del paciente:")
        return CITA_NOMBRE


async def elegir_horario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    elegido = update.message.text.strip()
    slots = context.user_data.get('slots', [])
    slot = next((s for s in slots if s['label'] == elegido), None)
    if not slot:
        await update.message.reply_text("Por favor selecciona un horario válido de la lista.")
        return CITAS_ELEGIR_HORARIO

    nombre = context.user_data.get('nombre') or (context.user_data.get('paciente_sheet') or {}).get('Nombre', '')
    documento = context.user_data.get('documento', '')
    correo = context.user_data.get('correo') or (context.user_data.get('paciente_sheet') or {}).get('Correo', '')

    summary = f"Consulta - {nombre}" if nombre else "Consulta"
    description = f"Documento: {documento}\nCorreo: {correo}\nCreado por bot"

    try:
        event_id = calendar_create_event(slot['start'], slot['end'], summary, description)
    except Exception as e:
        logger.exception(f"Error creando evento en Calendar: {e}")
        await update.message.reply_text("Ocurrió un error al agendar. Intenta nuevamente más tarde.")
        return await handle_policies(update, context)

    # Registrar en Sheets AgendaCitas
    try:
        tz = pytz.timezone(TIMEZONE)
        s_dt = date_parser.isoparse(slot['start']).astimezone(tz)
        e_dt = date_parser.isoparse(slot['end']).astimezone(tz)
        sheets_append_agenda({
            'Fecha': s_dt.strftime('%Y-%m-%d'),
            'HoraInicio': s_dt.strftime('%H:%M'),
            'HoraFin': e_dt.strftime('%H:%M'),
            'Documento': documento,
            'Nombre': nombre,
            'CalendarEventId': event_id,
            'Estado': 'Agendado',
            'CreadoEn': datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S'),
        })
    except Exception as e:
        logger.exception(f"Error guardando en AgendaCitas: {e}")

    await update.message.reply_text(
        f"✅ Cita agendada para {elegido}.\nID de evento: {event_id}",
        reply_markup=ReplyKeyboardRemove()
    )
    return await handle_policies(update, context)

def main() -> None:
    """Ejecutar el bot"""
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    print("TOKEN:", telegram_bot_token)

    if not telegram_bot_token:
        # Esto es importante para que el bot no intente iniciar sin el token
        logger.error("Error: La variable de entorno TELEGRAM_BOT_TOKEN no está configurada.")
        # Opcional: puedes salir del programa si el token es crítico
        import sys
        sys.exit(1)
        
    application = Application.builder().token(telegram_bot_token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start),
                    CommandHandler('menu', start_menu)],
        states={
            MENU_ES: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_es)],
            POLICIES: [MessageHandler(filters.Regex("(?i)^sí.*|^si.*|^no$"), handle_confirmation)],
            CITA_NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_nombre)],
            CITA_NACIMIENTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_nacimiento)],
            CITA_EDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_edad)],
            CITA_DOCUMENTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_documento)],
            CITA_OCUPACION: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_ocupacion)],
            CITA_REFERIDO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_referido)],
            CITA_TELFIJO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_telefono_fijo)],
            CITA_CELULAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_celular)],
            CITA_CORREO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_correo)],
            CITA_DIRECCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, cita_direccion)],
            TRATAMIENTO_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, tratamientos_info)],
            TRATAMIENTO_INFO: [MessageHandler(filters.Regex("^(Sí|No)$"), tratamientos_continuar)],
            PRECIOS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, precios_info)],
            PRECIOS_DECISION: [MessageHandler(filters.TEXT & ~filters.COMMAND, precios_siguiente)],
            EDUCACION_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, educacion_info)],
            EDUCACION_DECISION: [MessageHandler(filters.TEXT & ~filters.COMMAND, educacion_siguiente)],
            CONTACTO_OPCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, contacto_respuesta)],
            SUERO_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, suero_categoria)],
            SUERO_BIENESTAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, suero_info)],
            SUERO_HORMONAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, suero_info)],
            SUERO_POSTQX: [MessageHandler(filters.TEXT & ~filters.COMMAND, suero_info)],
            SUERO_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, suero_info)],
            CITAS_CONFIRMAR_PACIENTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_paciente)],
            CITAS_ELEGIR_HORARIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, elegir_horario)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)

    application.add_handler(CommandHandler("cancel", cancel))
    logger.info("Bot iniciado. Esperando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
