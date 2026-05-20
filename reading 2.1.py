"""
Reading Accuracy and Comprehension Assessment for Primary Students
Streamlit version with prepared passages, fixed questions per passage,
and text-to-speech playback for each short story.

Install:
    pip install streamlit SpeechRecognition gTTS pydub

Note: For audio processing, ffmpeg may need to be installed separately if pydub encounters issues.
On Windows, you can install ffmpeg from https://ffmpeg.org/download.html or use chocolatey: choco install ffmpeg

Run:
    python -m streamlit run reading_assessment_app.py
"""

import io
import re
import wave
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher

import streamlit as st
from gtts import gTTS
from pydub import AudioSegment

try:
    import speech_recognition as sr
    SR_AVAILABLE = True
except Exception:
    sr = None
    SR_AVAILABLE = False


# -----------------------------
# Database functions for teachers and students
# -----------------------------
def init_database():
    """Initialize the database and create tables if they don't exist."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    
    # Admins table
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_date TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Teachers table
    c.execute('''CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        municipality TEXT,
        school_name TEXT,
        created_date TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Ensure old database has the new teacher columns
    c.execute("PRAGMA table_info(teachers)")
    columns = [row[1] for row in c.fetchall()]
    if 'municipality' not in columns:
        c.execute('ALTER TABLE teachers ADD COLUMN municipality TEXT')
    if 'school_name' not in columns:
        c.execute('ALTER TABLE teachers ADD COLUMN school_name TEXT')
    
    # Students table
    c.execute('''CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        class TEXT,
        teacher_id INTEGER,
        created_date TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (teacher_id) REFERENCES teachers (id)
    )''')
    
    # Assessments table
    c.execute('''CREATE TABLE IF NOT EXISTS assessments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        teacher_id INTEGER,
        passage_title TEXT NOT NULL,
        accuracy_score REAL,
        pronunciation_score REAL,
        fluency_score REAL,
        comprehension_score REAL,
        words_omitted TEXT DEFAULT '',
        words_added TEXT DEFAULT '',
        words_substituted TEXT DEFAULT '',
        assessment_date TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students (id),
        FOREIGN KEY (teacher_id) REFERENCES teachers (id)
    )''')
    
    conn.commit()
    conn.close()


def add_teacher(name: str, email: str, password: str, municipality: str = '', school_name: str = '') -> bool:
    """Add a new teacher to the database."""
    try:
        conn = sqlite3.connect('reading_assessment.db')
        c = conn.cursor()
        c.execute('INSERT INTO teachers (name, email, password_hash, municipality, school_name) VALUES (?, ?, ?, ?, ?)',
                 (name, email, password, municipality, school_name))  # In production, hash the password
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False  # Email already exists


def add_student(name: str, class_name: str, teacher_id: int) -> int:
    """Add a new student and return the student ID."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('INSERT INTO students (name, class, teacher_id) VALUES (?, ?, ?)',
             (name, class_name, teacher_id))
    student_id = c.lastrowid
    conn.commit()
    conn.close()
    return student_id


def save_assessment(student_id: int, teacher_id: int, passage_title: str, 
                   accuracy: float, pronunciation: float, fluency: float, 
                   comprehension: float, omitted_words: list = None, 
                   added_words: list = None, substituted_words: list = None):
    """Save assessment results to database."""
    omitted_words = omitted_words or []
    added_words = added_words or []
    substituted_words = substituted_words or []
    
    # Convert word lists to comma-separated strings
    omitted_str = ', '.join(omitted_words) if omitted_words else ''
    added_str = ', '.join(added_words) if added_words else ''
    substituted_str = ', '.join([f"{orig}->{sub}" for orig, sub in substituted_words]) if substituted_words else ''
    
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('''INSERT INTO assessments 
                 (student_id, teacher_id, passage_title, accuracy_score, 
                  pronunciation_score, fluency_score, comprehension_score,
                  words_omitted, words_added, words_substituted)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
             (student_id, teacher_id, passage_title, accuracy, pronunciation, 
              fluency, comprehension, omitted_str, added_str, substituted_str))
    conn.commit()
    conn.close()


def get_students_for_teacher(teacher_id: int) -> list:
    """Get all students for a specific teacher."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('SELECT id, name, class FROM students WHERE teacher_id = ?', (teacher_id,))
    students = c.fetchall()
    conn.close()
    return students


def get_assessments_for_student(student_id: int) -> list:
    """Get all assessments for a specific student."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('''SELECT passage_title, accuracy_score, pronunciation_score, 
                        fluency_score, comprehension_score, assessment_date
                 FROM assessments 
                 WHERE student_id = ? 
                 ORDER BY assessment_date DESC''', (student_id,))
    assessments = c.fetchall()
    conn.close()
    return assessments


def delete_student(student_id: int, teacher_id: int) -> bool:
    """Delete a student and all their assessments. Returns True if successful."""
    try:
        conn = sqlite3.connect('reading_assessment.db')
        c = conn.cursor()
        
        # First delete all assessments for this student
        c.execute('DELETE FROM assessments WHERE student_id = ? AND teacher_id = ?', (student_id, teacher_id))
        
        # Then delete the student
        c.execute('DELETE FROM students WHERE id = ? AND teacher_id = ?', (student_id, teacher_id))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error deleting student: {e}")
        return False


def get_teacher_dashboard_data(teacher_id: int) -> dict:
    """Get dashboard data for teacher including student count and recent assessments."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    
    # Count students
    c.execute('SELECT COUNT(*) FROM students WHERE teacher_id = ?', (teacher_id,))
    student_count = c.fetchone()[0]
    
    # Count assessments
    c.execute('SELECT COUNT(*) FROM assessments WHERE teacher_id = ?', (teacher_id,))
    assessment_count = c.fetchone()[0]
    
    # Recent assessments
    c.execute('''SELECT s.name, a.passage_title, a.accuracy_score, a.assessment_date
                 FROM assessments a
                 JOIN students s ON a.student_id = s.id
                 WHERE a.teacher_id = ?
                 ORDER BY a.assessment_date DESC
                 LIMIT 10''', (teacher_id,))
    recent_assessments = c.fetchall()
    
    conn.close()
    
    return {
        'student_count': student_count,
        'assessment_count': assessment_count,
        'recent_assessments': recent_assessments
    }


def authenticate_teacher(email: str, password: str) -> tuple:
    """Authenticate teacher and return (success, teacher_id, teacher_name)."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('SELECT id, name FROM teachers WHERE email = ? AND password_hash = ?',
             (email, password))  # In production, verify hashed password
    result = c.fetchone()
    conn.close()
    
    if result:
        return True, result[0], result[1]
    return False, None, None


def check_if_teachers_exist() -> bool:
    """Check if any teachers are registered in the database."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM teachers')
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def add_admin(name: str, email: str, password: str) -> bool:
    """Add a new admin account."""
    try:
        conn = sqlite3.connect('reading_assessment.db')
        c = conn.cursor()
        c.execute('INSERT INTO admins (name, email, password_hash) VALUES (?, ?, ?)',
                 (name, email, password))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False


def authenticate_admin(email: str, password: str) -> bool:
    """Authenticate admin and return True if credentials are valid."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('SELECT id FROM admins WHERE email = ? AND password_hash = ?', (email, password))
    result = c.fetchone()
    conn.close()
    return bool(result)


def check_if_admins_exist() -> bool:
    """Check if any admin accounts are registered in the database."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM admins')
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def get_all_teachers() -> list:
    """Return all teacher records."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('SELECT id, name, email, municipality, school_name FROM teachers ORDER BY name')
    teachers = c.fetchall()
    conn.close()
    return teachers


def get_teacher_by_id(teacher_id: int):
    """Return a teacher record by ID."""
    conn = sqlite3.connect('reading_assessment.db')
    c = conn.cursor()
    c.execute('SELECT id, name, email, password_hash, municipality, school_name FROM teachers WHERE id = ?', (teacher_id,))
    teacher = c.fetchone()
    conn.close()
    return teacher


def update_teacher_profile(teacher_id: int, name: str, email: str, municipality: str = '', school_name: str = '', password: str | None = None) -> bool:
    """Update teacher name, email, municipality, school name, and optionally password."""
    try:
        conn = sqlite3.connect('reading_assessment.db')
        c = conn.cursor()
        if password:
            c.execute('''UPDATE teachers SET name = ?, email = ?, password_hash = ?, municipality = ?, school_name = ? WHERE id = ?''',
                      (name, email, password, municipality, school_name, teacher_id))
        else:
            c.execute('''UPDATE teachers SET name = ?, email = ?, municipality = ?, school_name = ? WHERE id = ?''',
                      (name, email, municipality, school_name, teacher_id))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False


# Initialize database on startup
init_database()


# -----------------------------
# Utility functions
# -----------------------------
def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-zA-Z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def word_list(text: str):
    return normalize_text(text).split()


def reading_accuracy_score(expected: str, actual: str) -> float:
    expected_words = word_list(expected)
    actual_words = word_list(actual)

    if not expected_words:
        return 0.0

    matcher = SequenceMatcher(None, expected_words, actual_words)
    return round(matcher.ratio() * 100, 2)


def pronunciation_score(expected: str, actual: str) -> float:
    """
    Calculate pronunciation score based on phonetic similarity.
    Uses Levenshtein distance normalized by expected text length.
    """
    expected_norm = normalize_text(expected)
    actual_norm = normalize_text(actual)

    if not expected_norm:
        return 0.0

    matcher = SequenceMatcher(None, expected_norm, actual_norm)
    # Get the ratio and convert to percentage
    score = matcher.ratio() * 100

    # Adjust score based on length differences (penalize significantly different lengths)
    expected_len = len(expected_norm.split())
    actual_len = len(actual_norm.split())

    if expected_len > 0:
        length_ratio = min(actual_len, expected_len) / max(actual_len, expected_len)
        score = score * length_ratio

    return round(score, 2)


def calculate_fluency_wpm(audio_bytes: bytes, word_count: int) -> tuple[float, str]:
    """
    Calculate words per minute from audio duration and word count.
    Returns (wpm, error_message) where error_message is None if successful.
    """
    try:
        audio_buffer = io.BytesIO(audio_bytes)

        # First try using wave module for WAV files (streamlit audio_input default)
        duration_seconds = None
        try:
            audio_buffer.seek(0)
            wav_file = wave.open(audio_buffer, 'rb')
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
            duration_seconds = frames / float(rate)
            wav_file.close()
        except:
            pass

        # If wave module didn't work, try pydub
        if duration_seconds is None:
            audio_buffer.seek(0)
            # Try different formats since streamlit audio_input format may vary
            audio = None
            for format_func in [AudioSegment.from_wav, AudioSegment.from_mp3, AudioSegment.from_file]:
                try:
                    audio_buffer.seek(0)  # Reset buffer position
                    if format_func == AudioSegment.from_file:
                        audio = format_func(audio_buffer)
                    else:
                        audio = format_func(audio_buffer)
                    break
                except:
                    continue

            if audio is None:
                return 0.0, "Could not load audio file - unsupported format"

            duration_seconds = len(audio) / 1000.0  # pydub gives duration in milliseconds

        if duration_seconds > 0 and word_count > 0:
            wpm = (word_count / duration_seconds) * 60
            return round(wpm, 2), None
        elif duration_seconds == 0:
            return 0.0, "Audio duration is zero"
        else:
            return 0.0, "No words detected in transcription"
    except Exception as e:
        return 0.0, f"Audio processing error: {str(e)}"


def fluency_assessment(wpm: float) -> str:
    """
    Assess reading fluency based on words per minute.
    """
    if wpm >= 150:
        return "Excellent fluency"
    elif wpm >= 120:
        return "Good fluency"
    elif wpm >= 90:
        return "Average fluency"
    elif wpm >= 60:
        return "Below average fluency"
    else:
        return "Poor fluency"


def analyze_word_errors(expected: str, actual: str):
    expected_words = word_list(expected)
    actual_words = word_list(actual)

    omitted = []
    added = []
    substitutions = []

    i = 0
    j = 0

    while i < len(expected_words) and j < len(actual_words):
        if expected_words[i] == actual_words[j]:
            i += 1
            j += 1
        else:
            if i + 1 < len(expected_words) and expected_words[i + 1] == actual_words[j]:
                omitted.append(expected_words[i])
                i += 1
            elif j + 1 < len(actual_words) and expected_words[i] == actual_words[j + 1]:
                added.append(actual_words[j])
                j += 1
            else:
                substitutions.append((expected_words[i], actual_words[j]))
                i += 1
                j += 1

    while i < len(expected_words):
        omitted.append(expected_words[i])
        i += 1

    while j < len(actual_words):
        added.append(actual_words[j])
        j += 1

    return omitted, added, substitutions


# -----------------------------
# Text-to-Speech
# -----------------------------
def generate_story_audio(text: str):
    """
    Convert passage text to playable MP3 bytes.
    """
    try:
        tts = gTTS(text=text, lang="en", slow=True)  # slow=True for primary learners
        audio_buffer = io.BytesIO()
        tts.write_to_fp(audio_buffer)
        audio_buffer.seek(0)
        return audio_buffer
    except Exception as e:
        return None


# -----------------------------
# Speech transcription
# -----------------------------
def transcribe_audio_file(uploaded_audio):
    if not SR_AVAILABLE:
        return "", "SpeechRecognition is not installed or failed to load."

    try:
        recognizer = sr.Recognizer()

        audio_bytes = uploaded_audio.getvalue()
        audio_buffer = io.BytesIO(audio_bytes)

        with sr.AudioFile(audio_buffer) as source:
            audio_data = recognizer.record(source)

        text = recognizer.recognize_google(audio_data)
        return text, None

    except sr.UnknownValueError:
        return "", "Speech was not understood. Please record again."
    except sr.RequestError:
        return "", "Speech recognition service is unavailable. Check internet connection."
    except Exception as e:
        return "", f"Transcription error: {e}"


def check_multiple_choice_answer(selected_option: str, correct_option: str):
    return normalize_text(selected_option) == normalize_text(correct_option)


# -----------------------------
# Prepared passages and questions
# -----------------------------
PASSAGE_BANK = {
    "Passage 1 - The Boy Goes to School": {
        "passage": (
            "The boy walks to school every morning. "
            "He carries his bag and smiles at his teacher."
        ),
        "questions": [
            {
                "question": "1. Where does the boy go every morning?",
                "options": ["To school", "To the market", "To the park", "To the farm"],
                "correct": "To school"
            },
            {
                "question": "2. What does the boy carry?",
                "options": ["A toy", "A bag", "A ball", "A stick"],
                "correct": "A bag"
            },
            {
                "question": "3. When does the boy walk to school?",
                "options": ["Every morning", "Every night", "Every Sunday", "Every afternoon"],
                "correct": "Every morning"
            },
            {
                "question": "4. What does the boy do to his teacher?",
                "options": ["He smiles", "He shouts", "He hides", "He cries"],
                "correct": "He smiles"
            },
            {
                "question": "5. Who does the boy smile at?",
                "options": ["His teacher", "His brother", "His friend", "His father"],
                "correct": "His teacher"
            },
        ]
    },

    "Passage 2 - Ana and the Garden": {
        "passage": (
            "Ana waters the plants in the garden every afternoon. "
            "She uses a small blue watering can and checks the flowers carefully."
        ),
        "questions": [
            {
                "question": "1. What does Ana do every afternoon?",
                "options": ["Waters the plants", "Feeds the dog", "Cleans the room", "Reads a book"],
                "correct": "Waters the plants"
            },
            {
                "question": "2. Where does Ana work?",
                "options": ["In the garden", "In the kitchen", "In the classroom", "In the market"],
                "correct": "In the garden"
            },
            {
                "question": "3. What does Ana use?",
                "options": ["A small blue watering can", "A red basket", "A long broom", "A school bag"],
                "correct": "A small blue watering can"
            },
            {
                "question": "4. When does Ana water the plants?",
                "options": ["Every afternoon", "Every morning", "At night", "On weekends only"],
                "correct": "Every afternoon"
            },
            {
                "question": "5. What does Ana check carefully?",
                "options": ["The flowers", "The chairs", "The books", "The toys"],
                "correct": "The flowers"
            },
        ]
    },

    "Passage 3 - Ben and His Dog": {
        "passage": (
            "Ben plays with his dog after class. "
            "They run near the big tree and share a happy time together."
        ),
        "questions": [
            {
                "question": "1. Who plays after class?",
                "options": ["Ben", "Ana", "The teacher", "The farmer"],
                "correct": "Ben"
            },
            {
                "question": "2. Who does Ben play with?",
                "options": ["His dog", "His cat", "His brother", "His classmate"],
                "correct": "His dog"
            },
            {
                "question": "3. When does Ben play with his dog?",
                "options": ["After class", "Before breakfast", "At midnight", "Every morning"],
                "correct": "After class"
            },
            {
                "question": "4. Where do they run?",
                "options": ["Near the big tree", "Inside the house", "At the river", "In the store"],
                "correct": "Near the big tree"
            },
            {
                "question": "5. What kind of time do they share?",
                "options": ["A happy time", "A sad time", "A sleepy time", "A quiet test"],
                "correct": "A happy time"
            },
        ]
    }
}


# -----------------------------
# Streamlit App
# -----------------------------
st.set_page_config(
    page_title="Reading Accuracy Prototype",
    page_icon="📘",
    layout="centered"
)

st.title("📘 Reading Accuracy and Comprehension Assessment")
st.write("Prototype for primary students using prepared short stories, speech recognition, and passage-based comprehension questions.")

# -----------------------------
# Role-based Access Function with Database
# -----------------------------
def admin_role_handler():
    """
    Handle admin functions for teachers and students with database integration.
    Teachers can authenticate and manage students.
    Students can access reading exercises.
    """
    role = st.sidebar.selectbox(
        "Select Your Role",
        ["Guest Student", "Teacher", "Admin"],
        help="Choose your role to access appropriate features"
    )
    
    if role == "Admin":
        if not check_if_admins_exist():
            st.sidebar.subheader("🔐 First Admin Registration")
            st.sidebar.info("No admin accounts exist yet. Create the first admin to manage teacher profiles.")

            with st.sidebar.form("register_first_admin"):
                admin_name = st.text_input("Full Name")
                admin_email = st.text_input("Email")
                admin_password = st.text_input("Password", type="password")
                admin_confirm = st.text_input("Confirm Password", type="password")
                submitted = st.form_submit_button("Register Admin")

                if submitted:
                    if admin_password != admin_confirm:
                        st.sidebar.error("Passwords do not match")
                    elif not admin_name or not admin_email or not admin_password:
                        st.sidebar.error("All fields are required")
                    else:
                        if add_admin(admin_name, admin_email, admin_password):
                            st.sidebar.success("Admin account created. Please login.")
                            st.rerun()
                        else:
                            st.sidebar.error("Email already exists")
            return "admin_registration"

        st.sidebar.subheader("🛠 Admin Login")

        admin_email = st.sidebar.text_input("Email", key="admin_email")
        admin_password = st.sidebar.text_input("Password", type="password", key="admin_password")

        col1, col2 = st.sidebar.columns(2)
        with col1:
            login_clicked = st.button("Login", key="admin_login")
        with col2:
            if st.button("Logout", key="admin_logout"):
                if 'admin_authenticated' in st.session_state:
                    del st.session_state.admin_authenticated
                st.rerun()

        if login_clicked:
            if authenticate_admin(admin_email, admin_password):
                st.sidebar.success("Admin logged in successfully.")
                st.session_state.admin_authenticated = True
                st.rerun()
            else:
                st.sidebar.error("Invalid admin credentials")
                return "admin_unauthenticated"

        if 'admin_authenticated' in st.session_state and st.session_state.admin_authenticated:
            return "admin"
        else:
            st.sidebar.info("Please login to access admin features")
            return "admin_unauthenticated"

    elif role == "Teacher":
        # Check if any teachers exist
        if not check_if_teachers_exist():
            st.sidebar.subheader("👨‍🏫 First Teacher Registration")
            st.sidebar.info("No teachers registered yet. Please create the first teacher account.")
            
            with st.sidebar.form("register_first_teacher"):
                reg_name = st.text_input("Full Name")
                reg_email = st.text_input("Email")
                reg_password = st.text_input("Password", type="password")
                reg_confirm = st.text_input("Confirm Password", type="password")
                reg_municipality = st.text_input("Municipality")
                reg_school = st.text_input("Name of School")
                submitted = st.form_submit_button("Register First Teacher")
                
                if submitted:
                    if reg_password != reg_confirm:
                        st.sidebar.error("Passwords do not match")
                    elif not reg_name or not reg_email or not reg_password:
                        st.sidebar.error("All fields are required")
                    else:
                        if add_teacher(reg_name, reg_email, reg_password, reg_municipality, reg_school):
                            st.sidebar.success("First teacher registered successfully! Please login.")
                            st.rerun()
                        else:
                            st.sidebar.error("Email already exists")
            return "teacher_registration"
        
        st.sidebar.subheader("👨‍🏫 Teacher Login")
        
        # Teacher authentication
        email = st.sidebar.text_input("Email", key="teacher_email")
        password = st.sidebar.text_input("Password", type="password", key="teacher_password")
        
        col1, col2 = st.sidebar.columns(2)
        with col1:
            login_clicked = st.button("Login", key="teacher_login")
        with col2:
            if st.button("Logout", key="teacher_logout"):
                if 'authenticated' in st.session_state:
                    del st.session_state.authenticated
                    del st.session_state.teacher_id
                    del st.session_state.teacher_name
                st.rerun()
        
        if login_clicked:
            success, teacher_id, teacher_name = authenticate_teacher(email, password)
            if success:
                st.sidebar.success(f"Welcome, {teacher_name}!")
                st.session_state.teacher_id = teacher_id
                st.session_state.teacher_name = teacher_name
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.sidebar.error("Invalid credentials")
                return "teacher_unauthenticated"
        
        # Check if authenticated
        if 'authenticated' in st.session_state and st.session_state.authenticated:
            return "teacher"
        else:
            st.sidebar.info("Please login to access teacher features")
            return "teacher_unauthenticated"
    
    else:  # Student
        st.sidebar.info("👨‍🎓 Student Mode: Access to reading exercises")
        return "student"


# Get user role

def student_assessment_portal():
    st.subheader("👩‍🎓 Student Reading Practice")
    selected_title = st.selectbox("Choose a Short Story", list(PASSAGE_BANK.keys()), key="student_passage")
    selected_passage_data = PASSAGE_BANK[selected_title]
    expected_passage = selected_passage_data["passage"]
    selected_questions = selected_passage_data["questions"]

    st.write("**Short Story:**")
    st.info(expected_passage)

    story_audio = generate_story_audio(expected_passage)
    if story_audio is not None:
        st.audio(story_audio, format="audio/mp3")
    else:
        st.warning("Audio preview could not be generated. Please check your internet connection.")

    st.write("Upload a recording of the student's reading or type the reading manually.")
    student_audio = st.file_uploader(
        "Student reading audio file (WAV/MP3)",
        type=["wav", "mp3"],
        key="student_audio"
    )
    student_manual = st.text_area(
        "Manual reading text",
        placeholder="Type the student's reading here if audio is not available.",
        height=120,
        key="student_manual"
    )

    student_answers = []
    st.subheader("Reading Comprehension Questions")
    for i, item in enumerate(selected_questions):
        st.markdown(f"### Question {i+1}")
        st.write(item["question"])
        answer = st.radio(
            f"Student's Answer for Question {i+1}",
            item["options"],
            key=f"student_q{i}"
        )
        student_answers.append(answer)

    if st.button("Submit Student Assessment", key="student_submit_assessment"):
        if student_audio is None and not student_manual.strip():
            st.error("Please upload audio or enter the student's reading manually.")
            return

        spoken_text = ""
        error = None

        if student_audio is not None:
            spoken_text, error = transcribe_audio_file(student_audio)
            if error and student_manual.strip():
                spoken_text = student_manual.strip()
                error = None
        else:
            spoken_text = student_manual.strip()

        if error:
            st.error(error)
            return

        accuracy = reading_accuracy_score(expected_passage, spoken_text)
        pronunciation = pronunciation_score(expected_passage, spoken_text)
        omitted, added, substitutions = analyze_word_errors(expected_passage, spoken_text)

        fluency_wpm = 0.0
        fluency_level = "Not available"
        if student_audio is not None:
            word_count = len(word_list(spoken_text))
            fluency_wpm, fluency_error = calculate_fluency_wpm(student_audio.getvalue(), word_count)
            if fluency_error:
                fluency_level = f"Error: {fluency_error}"
            else:
                fluency_level = fluency_assessment(fluency_wpm)

        st.success("Assessment complete.")
        st.write("**Recognized / Entered Reading:**")
        st.write(spoken_text)

        st.metric("Reading Accuracy Score", f"{accuracy}%")
        st.metric("Pronunciation Score", f"{pronunciation}%")
        if student_audio is not None:
            fluency_percent = min(round((fluency_wpm / 150) * 100, 1), 100.0)
            st.metric("Reading Fluency (WPM)", f"{fluency_wpm} - {fluency_level} ({fluency_percent}%)")

        with st.expander("Show Reading Error Analysis"):
            st.write("**Omitted Words:**", omitted if omitted else "None")
            st.write("**Added Words:**", added if added else "None")
            st.write("**Substituted Words:**", substitutions if substitutions else "None")

        total_score = 0
        results = []
        for i, item in enumerate(selected_questions):
            is_correct = check_multiple_choice_answer(student_answers[i], item["correct"])
            if is_correct:
                total_score += 1
            results.append({
                "question": item["question"],
                "student_answer": student_answers[i],
                "correct_answer": item["correct"],
                "result": "Correct" if is_correct else "Incorrect"
            })

        comprehension_percentage = round((total_score / len(selected_questions)) * 100, 2)
        st.metric("Comprehension Score", f"{comprehension_percentage}%")
        st.write(f"Total Correct Answers: {total_score}/{len(selected_questions)}")

        for idx, result in enumerate(results, start=1):
            with st.expander(f"Result for Question {idx}"):
                st.write("**Question:**", result["question"])
                st.write("**Student Answer:**", result["student_answer"])
                st.write("**Correct Answer:**", result["correct_answer"])
                st.write("**Result:**", result["result"])

# -----------------------------
# Get user role
user_role = admin_role_handler()

# Show different content based on role
if user_role == "teacher":
    teacher_id = st.session_state.teacher_id
    teacher_name = st.session_state.teacher_name
    
    st.write(f"**Welcome, {teacher_name}!**")
    
    # Teacher Dashboard
    st.subheader("📊 Teacher Dashboard")
    dashboard_data = get_teacher_dashboard_data(teacher_id)
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Students", dashboard_data['student_count'])
    with col2:
        st.metric("Total Assessments", dashboard_data['assessment_count'])
    with col3:
        avg_score = 0  # Could calculate average score
        st.metric("Average Score", f"{avg_score}%")
    
    # Student Management
    st.subheader("👥 Student Management")
    tab1, tab2, tab3 = st.tabs(["Manage Students", "New Assessment", "Assessment Records"])
    
    with tab1:
        # Add Student Section
        st.subheader("➕ Add New Student")
        with st.form("add_student_form"):
            student_name = st.text_input("Student Name")
            student_class = st.text_input("Class/Grade")
            submitted = st.form_submit_button("Add Student")
            if submitted and student_name:
                student_id = add_student(student_name, student_class, teacher_id)
                st.success(f"Student '{student_name}' added successfully!")
                st.rerun()
        
        st.divider()
        
        # View Students Section
        st.subheader("👨‍🎓 View Students")
        students = get_students_for_teacher(teacher_id)
        if students:
            st.write("Your Students:")
            for student in students:
                with st.expander(f"{student[1]} (Class: {student[2]})"):
                    # Delete button with confirmation
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        assessments = get_assessments_for_student(student[0])
                        if assessments:
                            st.write("Recent Assessments:")
                            for assessment in assessments[:3]:  # Show last 3
                                st.write(f"• {assessment[0]}: Accuracy {assessment[1]}%, Pronunciation {assessment[2]}%, Comprehension {assessment[4]}%")
                        else:
                            st.write("No assessments yet.")
                    
                    with col2:
                        if st.button("🗑️ Delete", key=f"delete_{student[0]}", help=f"Delete student {student[1]}"):
                            # Show confirmation dialog
                            st.warning(f"Are you sure you want to delete student '{student[1]}'? This will also delete all their assessment records.")
                            col_confirm, col_cancel = st.columns(2)
                            with col_confirm:
                                if st.button("Yes, Delete", key=f"confirm_delete_{student[0]}"):
                                    if delete_student(student[0], teacher_id):
                                        st.success(f"Student '{student[1]}' and all their assessments have been deleted.")
                                        st.rerun()
                                    else:
                                        st.error("Failed to delete student. Please try again.")
                            with col_cancel:
                                if st.button("Cancel", key=f"cancel_delete_{student[0]}"):
                                    st.rerun()
        else:
            st.write("No students added yet.")
    
    with tab2:
        st.subheader("📝 New Assessment")
        
        # Select student for assessment
        students = get_students_for_teacher(teacher_id)
        if students:
            student_options = ["Select Student"] + [f"{s[1]} (ID: {s[0]})" for s in students]
            selected_student = st.selectbox("Select Student for Assessment", student_options, key="new_assessment_student")
            
            if selected_student != "Select Student":
                student_id = int(selected_student.split("(ID: ")[1].rstrip(")"))
                st.success(f"Assessing: {selected_student.split(' (ID:')[0]}")
                
                # -----------------------------
                # Select prepared passage
                # -----------------------------
                st.subheader("Step 1: Select a Prepared Short Story")

                selected_title = st.selectbox(
                    "Choose a Short Story",
                    list(PASSAGE_BANK.keys()),
                    key="new_assessment_passage"
                )

                selected_passage_data = PASSAGE_BANK[selected_title]
                expected_passage = selected_passage_data["passage"]
                selected_questions = selected_passage_data["questions"]

                st.write("**Short Story:**")
                st.info(expected_passage)

                # -----------------------------
                # Listen to the story
                # -----------------------------
                st.subheader("Listen to the Story")
                st.write("The student may listen first before reading aloud.")

                story_audio = generate_story_audio(expected_passage)

                if story_audio is not None:
                    st.audio(story_audio, format="audio/mp3")
                else:
                    st.warning("Audio preview could not be generated. Please check your internet connection.")

                # Assessment Section (only show if student is selected)
                if student_id is not None:
                    # -----------------------------
                    # Step 2: Reading Accuracy
                    # -----------------------------
                    st.subheader("Step 2: Upload and Assess Reading Accuracy")
                    st.write("Upload the student's recorded reading or enter the text manually.")

                    audio_value = st.file_uploader(
                        "Upload student reading audio file (WAV/MP3)",
                        type=["wav", "mp3"],
                        key="new_assessment_audio"
                    )

                    manual_fallback = st.text_area(
                        "Manual fallback (optional)",
                        placeholder="If transcription fails, type or paste the student's reading here.",
                        height=100,
                        key="new_assessment_manual"
                    )

                    if st.button("Check Reading Accuracy", key="check_accuracy_btn"):
                        spoken_text = ""
                        error = None

                        if audio_value is not None:
                            spoken_text, error = transcribe_audio_file(audio_value)
                            if error and manual_fallback.strip():
                                spoken_text = manual_fallback.strip()
                                error = None
                        elif manual_fallback.strip():
                            spoken_text = manual_fallback.strip()
                        else:
                            error = "Please record audio or enter the student's reading manually."

                        if error:
                            st.error(error)
                        else:
                            st.success("Reading checked successfully.")
                            st.write("**Recognized / Entered Reading:**")
                            st.write(spoken_text)

                            accuracy = reading_accuracy_score(expected_passage, spoken_text)
                            pronunciation = pronunciation_score(expected_passage, spoken_text)
                            omitted, added, substitutions = analyze_word_errors(expected_passage, spoken_text)

                            # Calculate fluency if audio is available
                            fluency_wpm = 0.0
                            fluency_level = "Not available"
                            if audio_value is not None:
                                word_count = len(word_list(spoken_text))
                                fluency_wpm, fluency_error = calculate_fluency_wpm(audio_value.getvalue(), word_count)
                                if fluency_error:
                                    fluency_level = f"Error: {fluency_error}"
                                else:
                                    fluency_level = fluency_assessment(fluency_wpm)

                            st.metric("Reading Accuracy Score", f"{accuracy}%")
                            st.metric("Pronunciation Score", f"{pronunciation}%")
                            if audio_value is not None:
                                # Calculate fluency percentage (assuming 150 WPM is 100%)
                                fluency_percent = min(round((fluency_wpm / 150) * 100, 1), 100.0)
                                st.metric("Reading Fluency (WPM)", f"{fluency_wpm} - {fluency_level} ({fluency_percent}%)")

                            with st.expander("Show Reading Error Analysis"):
                                st.write("**Omitted Words:**", omitted if omitted else "None")
                                st.write("**Added Words:**", added if added else "None")
                                st.write("**Substituted Words:**", substitutions if substitutions else "None")

                            # Store results in session state for later saving
                            st.session_state.current_accuracy = accuracy
                            st.session_state.current_pronunciation = pronunciation
                            st.session_state.current_fluency = fluency_wpm
                            st.session_state.current_omitted_words = omitted
                            st.session_state.current_added_words = added
                            st.session_state.current_substituted_words = substitutions

                    # -----------------------------
                    # Step 3: Comprehension Questions
                    # -----------------------------
                    st.subheader("Step 3: Assess Reading Comprehension")
                    st.write("The questions below are automatically matched to the selected prepared passage.")

                    student_answers = []

                    for i, item in enumerate(selected_questions):
                        st.markdown(f"### Question {i+1}")
                        st.write(item["question"])

                        answer = st.radio(
                            f"Student's Answer for Question {i+1}",
                            item["options"],
                            key=f"new_assessment_q{i}"
                        )
                        student_answers.append(answer)

                    if st.button("Check Comprehension and Save Results", key="save_assessment_btn"):
                        total_score = 0
                        results = []

                        for i, item in enumerate(selected_questions):
                            is_correct = check_multiple_choice_answer(student_answers[i], item["correct"])
                            if is_correct:
                                total_score += 1

                            results.append({
                                "question": item["question"],
                                "student_answer": student_answers[i],
                                "correct_answer": item["correct"],
                                "result": "Correct" if is_correct else "Incorrect"
                            })

                        comprehension_percentage = round((total_score / len(selected_questions)) * 100, 2)

                        st.success("Comprehension checked successfully.")
                        st.metric("Total Correct Answers", f"{total_score}/{len(selected_questions)}")
                        st.metric("Comprehension Score", f"{comprehension_percentage}%")

                        for idx, result in enumerate(results, start=1):
                            with st.expander(f"Result for Question {idx}"):
                                st.write("**Question:**", result["question"])
                                st.write("**Student Answer:**", result["student_answer"])
                                st.write("**Correct Answer:**", result["correct_answer"])
                                st.write("**Result:**", result["result"])

                        # Save complete assessment to database
                        accuracy = st.session_state.get('current_accuracy', 0.0)
                        pronunciation = st.session_state.get('current_pronunciation', 0.0)
                        fluency = st.session_state.get('current_fluency', 0.0)
                        omitted_words = st.session_state.get('current_omitted_words', [])
                        added_words = st.session_state.get('current_added_words', [])
                        substituted_words = st.session_state.get('current_substituted_words', [])
                        
                        save_assessment(student_id, teacher_id, selected_title, 
                                      accuracy, pronunciation, fluency, comprehension_percentage,
                                      omitted_words, added_words, substituted_words)
                        
                        st.success("Assessment results saved to database!")
            else:
                st.warning("Please select a student to begin assessment.")
        else:
            st.warning("Please add students first in the Manage Students tab.")
    
    with tab3:
        st.subheader("📋 Assessment Records")
        
        # Get all assessments for this teacher
        conn = sqlite3.connect('reading_assessment.db')
        c = conn.cursor()
        c.execute('''SELECT a.id, s.name, a.passage_title, a.accuracy_score, 
                            a.pronunciation_score, a.fluency_score, a.comprehension_score,
                            a.words_omitted, a.words_added, a.words_substituted, a.assessment_date
                     FROM assessments a
                     JOIN students s ON a.student_id = s.id
                     WHERE a.teacher_id = ?
                     ORDER BY a.assessment_date DESC''', (teacher_id,))
        all_assessments = c.fetchall()
        conn.close()
        
        if all_assessments:
            # Filters
            col1, col2 = st.columns(2)
            with col1:
                students_list = list(set([assessment[1] for assessment in all_assessments]))
                selected_student_filter = st.selectbox(
                    "Filter by Student", 
                    ["All Students"] + students_list,
                    key="student_filter"
                )
            
            with col2:
                passages_list = list(set([assessment[2] for assessment in all_assessments]))
                selected_passage_filter = st.selectbox(
                    "Filter by Passage", 
                    ["All Passages"] + passages_list,
                    key="passage_filter"
                )
            
            # Filter the assessments
            filtered_assessments = all_assessments
            if selected_student_filter != "All Students":
                filtered_assessments = [a for a in filtered_assessments if a[1] == selected_student_filter]
            if selected_passage_filter != "All Passages":
                filtered_assessments = [a for a in filtered_assessments if a[2] == selected_passage_filter]
            
            # Display assessments in a table format
            if filtered_assessments:
                st.write(f"Showing {len(filtered_assessments)} assessment(s):")
                
                # Create a nice table display
                assessment_data = []
                for assessment in filtered_assessments:
                    fluency_percent = min(round((assessment[5] / 150) * 100, 1), 100.0)
                    assessment_data.append({
                        "Student": assessment[1],
                        "Passage": assessment[2],
                        "Accuracy (%)": f"{assessment[3]:.1f}",
                        "Pronunciation (%)": f"{assessment[4]:.1f}",
                        "Fluency (WPM)": f"{assessment[5]:.1f} ({fluency_percent}%)",
                        "Comprehension (%)": f"{assessment[6]:.1f}",
                        "Omitted Words": len(assessment[7].split(', ')) if assessment[7] else 0,
                        "Added Words": len(assessment[8].split(', ')) if assessment[8] else 0,
                        "Substituted Words": len(assessment[9].split(', ')) if assessment[9] else 0,
                        "Date": assessment[10][:10]  # Show only date part
                    })
                
                st.dataframe(assessment_data, use_container_width=True)
                
                # Add a new table for detailed reading error analysis
                st.subheader("📋 Reading Error Analysis")

                error_analysis_data = []
                for assessment in filtered_assessments:
                    error_analysis_data.append({
                        "Student": assessment[1],
                        "Passage": assessment[2],
                        "Omitted Words": assessment[7],
                        "Added Words": assessment[8],
                        "Substituted Words": assessment[9],
                        "Date": assessment[10][:10]  # Show only date part
                    })

                st.dataframe(error_analysis_data, use_container_width=True)
                
                # Summary statistics
                st.subheader("📈 Summary Statistics")
                if len(filtered_assessments) > 0:
                    avg_accuracy = sum([a[3] for a in filtered_assessments]) / len(filtered_assessments)
                    avg_pronunciation = sum([a[4] for a in filtered_assessments]) / len(filtered_assessments)
                    avg_fluency = sum([a[5] for a in filtered_assessments]) / len(filtered_assessments)
                    avg_comprehension = sum([a[6] for a in filtered_assessments]) / len(filtered_assessments)
                    
                    # Word error statistics - count words in strings
                    total_omitted = sum([len(a[7].split(', ')) if a[7] else 0 for a in filtered_assessments])
                    total_added = sum([len(a[8].split(', ')) if a[8] else 0 for a in filtered_assessments])
                    total_substituted = sum([len(a[9].split(', ')) if a[9] else 0 for a in filtered_assessments])
                    avg_omitted = total_omitted / len(filtered_assessments) if filtered_assessments else 0
                    avg_added = total_added / len(filtered_assessments) if filtered_assessments else 0
                    avg_substituted = total_substituted / len(filtered_assessments) if filtered_assessments else 0
                    
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric("Avg Accuracy", f"{avg_accuracy:.1f}%")
                    with col2:
                        st.metric("Avg Pronunciation", f"{avg_pronunciation:.1f}%")
                    with col3:
                        avg_fluency_percent = min(round((avg_fluency / 150) * 100, 1), 100.0)
                        st.metric("Avg Fluency", f"{avg_fluency:.1f} WPM ({avg_fluency_percent}%)")
                    with col4:
                        st.metric("Avg Comprehension", f"{avg_comprehension:.1f}%")
                    
                    # Word error analysis
                    st.subheader("📝 Word Error Analysis")
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric("Total Omitted Words", total_omitted)
                    with col2:
                        st.metric("Total Added Words", total_added)
                    with col3:
                        st.metric("Total Substitutions", total_substituted)
                    with col4:
                        st.metric("Total Word Errors", total_omitted + total_added + total_substituted)
                    
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.metric("Avg Omitted per Assessment", f"{avg_omitted:.1f}")
                    with col2:
                        st.metric("Avg Added per Assessment", f"{avg_added:.1f}")
                    with col3:
                        st.metric("Avg Substituted per Assessment", f"{avg_substituted:.1f}")
                    
                    # Most common error types
                    if total_omitted + total_added + total_substituted > 0:
                        error_types = ["Omitted", "Added", "Substituted"]
                        error_counts = [total_omitted, total_added, total_substituted]
                        most_common = error_types[error_counts.index(max(error_counts))]
                        st.info(f"**Most Common Error Type:** {most_common} words ({max(error_counts)} total)")
            else:
                st.info("No assessments match the selected filters.")
        else:
            st.info("No assessment records found. Start assessing students to see records here.")

elif user_role == "admin":
    st.subheader("🛡 Admin Portal")
    st.write("Manage teacher profiles and credentials from here.")

    teachers = get_all_teachers()
    tab1, tab2 = st.tabs(["Add Teacher", "Update Teacher"])

    with tab1:
        st.subheader("➕ Add a New Teacher")
        with st.form("admin_add_teacher_form"):
            new_teacher_name = st.text_input("Teacher Full Name", key="admin_new_teacher_name")
            new_teacher_email = st.text_input("Teacher Email", key="admin_new_teacher_email")
            new_teacher_password = st.text_input("Teacher Password", type="password", key="admin_new_teacher_password")
            new_teacher_confirm = st.text_input("Confirm Password", type="password", key="admin_new_teacher_confirm")
            new_teacher_municipality = st.text_input("Municipality", key="admin_new_teacher_municipality")
            new_teacher_school = st.text_input("Name of School", key="admin_new_teacher_school")
            submitted = st.form_submit_button("Create Teacher")

            if submitted:
                if new_teacher_password != new_teacher_confirm:
                    st.error("Passwords do not match.")
                elif not new_teacher_name or not new_teacher_email or not new_teacher_password:
                    st.error("Name, email, and password are required to create a teacher.")
                else:
                    if add_teacher(new_teacher_name, new_teacher_email, new_teacher_password, new_teacher_municipality, new_teacher_school):
                        st.success(f"Teacher '{new_teacher_name}' created successfully.")
                        st.rerun()
                    else:
                        st.error("A teacher with that email already exists.")

    with tab2:
        st.subheader("✏️ Update Teacher Profile")
        if teachers:
            teacher_options = [f"{t[1]} ({t[2]}) - ID {t[0]}" for t in teachers]
            selected_teacher = st.selectbox("Select Teacher to Update", ["Select a teacher"] + teacher_options, key="admin_select_teacher")

            if selected_teacher != "Select a teacher":
                teacher_id = int(selected_teacher.split(" - ID ")[1])
                teacher_record = get_teacher_by_id(teacher_id)

                if teacher_record:
                    update_name = st.text_input("Teacher Full Name", value=teacher_record[1], key="admin_update_name")
                    update_email = st.text_input("Teacher Email", value=teacher_record[2], key="admin_update_email")
                    update_municipality = st.text_input("Municipality", value=teacher_record[4] or "", key="admin_update_municipality")
                    update_school = st.text_input("Name of School", value=teacher_record[5] or "", key="admin_update_school")
                    update_password = st.text_input(
                        "New Password (leave blank to keep current)",
                        type="password",
                        key="admin_update_password"
                    )

                    if st.button("Update Teacher Profile", key="admin_update_teacher_btn"):
                        if not update_name or not update_email:
                            st.error("Name and email cannot be empty.")
                        else:
                            password_value = update_password.strip() if update_password.strip() else None
                            if update_teacher_profile(teacher_id, update_name, update_email, update_municipality, update_school, password_value):
                                st.success("Teacher profile updated successfully.")
                                st.rerun()
                            else:
                                st.error("Could not update teacher. The email may already be in use.")
        else:
            st.info("No teacher accounts exist yet. Use the Add Teacher tab to create one.")

    st.divider()
    st.subheader("📝 Existing Teacher Accounts")
    if teachers:
        table_data = []
        for teacher in teachers:
            table_data.append({
                "ID": teacher[0],
                "Name": teacher[1],
                "Email": teacher[2],
                "Municipality": teacher[3] or "",
                "School": teacher[4] or ""
            })
        st.dataframe(table_data, use_container_width=True)
    else:
        st.info("No teachers available yet.")

elif user_role == "student":
    student_assessment_portal()

elif user_role == "teacher_unauthenticated":
    st.warning("Please login as a teacher to access assessment features.")
    st.info("If this is your first time, the system will guide you through teacher registration.")

elif user_role == "teacher_registration":
    st.info("Teacher registration is handled in the sidebar. Please complete registration to continue.")

