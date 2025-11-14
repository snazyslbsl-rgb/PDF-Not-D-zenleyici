import streamlit as st
import fitz
import os
from google import genai
from google.genai import types, errors
from io import BytesIO
import time
from streamlit_cookies_manager import EncryptedCookieManager


st.set_page_config(page_title="PDF Akıllı Not Özetleyici", layout="wide")


st.markdown("""
<style>
.stApp {background-color: #1a1a1a; color: #e0e0e0; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;}
h1 { color: #64b5f6; font-family: 'Impact', 'Arial Black', sans-serif; text-align: center; padding-top: 20px; padding-bottom: 25px; text-shadow: 2px 2px 4px #000000; border-bottom: 4px solid #42a5f5; margin-bottom: 30px; }
h3 { color: #81c784; border-left: 5px solid #66bb6a; padding-left: 15px; padding-bottom: 0px; margin-top: 30px; font-weight: 600; font-size: 1.5em; }
.stButton>button { background-color: #ef5350; color: white; font-weight: bold; padding: 10px 20px; border-radius: 8px; border: none; transition: background-color 0.3s, transform 0.1s; min-width: 150px; }
.stButton>button:hover { background-color: #d32f2f; transform: scale(1.02); }
.stFileUploader { border: 2px dashed #42a5f5; padding: 20px; border-radius: 15px; background-color: #2e2e2e; margin-bottom: 20px; }
.stFileUploader label { color: #e0e0e0; font-weight: bold; }
.stDownloadButton > button { background-color: #42a5f5; }
.stDownloadButton > button:hover { background-color: #1976d2; }
div[data-testid="stAlert"] { border-radius: 8px; padding: 15px; font-weight: 500; color: #212121; }
div[data-testid="stAlert"].stAlert-info { background-color: #bbdefb; }
div[data-testid="stAlert"].stAlert-success { background-color: #c8e6c9; }
div[data-testid="stAlert"].stAlert-warning { background-color: #ffcc80; }
div[data-testid="stAlert"].stAlert-error { background-color: #ffcdd2; }
h2 { color: #64b5f6; text-align: center; margin-top: 40px; padding-bottom: 10px; }
.stNumberInput input { color: #e0e0e0; background-color: #3a3a3a; border: 1px solid #555555; }
div[data-testid="stVerticalBlock"] > div:has(div[data-testid="stContainer"]) { border-color: #444444; }
</style>
""", unsafe_allow_html=True)


MAX_FREE_SUMMARIES = 3
QUOTA_COOKIE_KEY = 'user_quota_used_v3'
ENCRYPTION_KEY = 'my-secret-key-for-cookies-1234567890'



cookies = EncryptedCookieManager(prefix="pdf_summarizer/", password=ENCRYPTION_KEY)

if not cookies.ready():
    cookies.ready()
    st.stop()

if 'quota_used' not in st.session_state:
    
    cookie_value = cookies.get(QUOTA_COOKIE_KEY) 
    
    if cookie_value is None:
        st.session_state['quota_used'] = 0 
    else:
       
        try:
            st.session_state['quota_used'] = int(cookie_value)
        except ValueError:
            st.session_state['quota_used'] = 0



@st.cache_data
def pdf_metni_cikar(uploaded_file, start_page=1, end_page=None):
    """Yüklenen PDF'den belirtilen sayfa aralığındaki metni çıkarır."""
    metin = ""
   
    uploaded_file.seek(0) 
    pdf_bytes = uploaded_file.read()
    
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as belge:
            toplam_sayfa = belge.page_count
            
           
            if start_page < 1 or start_page > toplam_sayfa:
                st.error(f"❌ Başlangıç sayfası ({start_page}) PDF sınırları dışında ({1}-{toplam_sayfa})")
                return None
            
            end_index = end_page if end_page and 0 < end_page <= toplam_sayfa else toplam_sayfa
            start_index = start_page - 1
            
            if start_index >= end_index:
                st.error("❌ Başlangıç sayfası, bitiş sayfasından sonra olamaz.")
                return None
            
            
            for i in range(start_index, end_index):
                sayfa = belge.load_page(i)
                metin += sayfa.get_text() + "\n---\n"
            
            if not metin.strip():
                st.error("❌ Metin çıkarılamadı veya seçilen aralık boş.")
                return None
            return metin
    except Exception as e:
        st.error(f"❌ PDF okuma hatası: {e}")
        return None


def metni_parcala(tum_metin, parca_boyutu=28000):
    """Metni AI bağlam limitini aşmayacak parçalara böler."""
    return [tum_metin[i:i+parca_boyutu] for i in range(0, len(tum_metin), parca_boyutu)]



def tam_ozetleme_sureci(tum_metin, max_retries=5):
    """Metni zincirleme veya tek parça halinde AI'ya gönderip özetleme yapar."""
    try:
        api_key = st.secrets["GEMINI_API_KEY"]
        
        client = genai.Client(api_key=api_key)
    except Exception as e:
        st.error(f"❌ AI istemcisi başlatılamadı veya API anahtarı bulunamadı: {e}")
        return None

    metin_parcalari = metni_parcala(tum_metin)

    
    nihai_komut_yapisi = """
    Aşağıdaki metin bir ders notu/akademik dokümandır. Tüm içeriği, öğrenmeyi kolaylaştıran, hiyerarşik ve yapısal bir rapora dönüştür. Raporun içinde:
    1. Ana başlıklar ve alt başlıklar.
    2. Her kavramın kısa, net açıklaması.
    3. Anahtar terimler, kalın yazılarak veya özetleyici kutular (Blockquotes) içinde vurgulanmalıdır.
    4. Gerektiğinde formüller veya kompleks değişkenler için LaTeX formatı ($...$ veya $$...$$) kullan.
    5. Metnin tonu akademik ve eğitici olmalıdır.
    """

    def ai_isteği_gonder(komut, model='gemini-2.5-pro', max_retries=5):
        """
        AI isteğini gönderir, 503 hatası alması durumunda daha uzun bekleyerek tekrar dener.
        """
        initial_delay=5

        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model, 
                    contents=komut
                )
                return response.text
            except errors.APIError as api_e:
                if '503 UNAVAILABLE' in str(api_e) and attempt < max_retries -1:

                    delay=initial_delay *( 2** attempt)

                    st.warning(f"⚠️ **Sunucu Yoğunluğu (503)**. {attempt + 1}/{max_retries}. deneme başarısız oldu. **{delay} saniye** bekleyip tekrar denenecek...")
                    time.sleep(delay)

                else: 
                    st.error(f"❌ Yapay Zeka isteği nihai olarak başarısız oldu (Deneme {attempt + 1}/{max_retries}): {api_e}")  
                    return None

            except Exception as e:
                st.error(f"❌ Beklenmedik Hata: {e}")
                return None

        st.error("🚨 Yapay zeka hizmeti, maksimum deneme hakkına rağmen kullanılamıyor. Lütfen daha sonra tekrar deneyin.")
        return None

    if len(metin_parcalari) > 1:
        
        st.info(f"PDF çok uzun. {len(metin_parcalari)} parça halinde işleniyor.")
        ara_ozetler = []
        progress_bar = st.progress(0, text="Parça İşleme Durumu: 0%")
        for i, parca in enumerate(metin_parcalari):
            ara_komut = f"Aşağıdaki metin bir akademik dokümanın parçasıdır. Bu parçayı, nihai birleştirme raporuna temel oluşturmak için en önemli 3-5 madde halinde özetle ve listele:\n{parca}"
            with st.spinner(f'⏳ Parça {i+1}/{len(metin_parcalari)} Özetleniyor...'):
                ozet_metni = ai_isteği_gonder(ara_komut)
                if ozet_metni is None: return None
                ara_ozetler.append(f"### Parça {i+1} Özeti\n{ozet_metni}\n\n---\n\n")
            progress_bar.progress((i+1)/len(metin_parcalari), text=f"Parça İşleme Durumu: %{int((i+1)/len(metin_parcalari)*100)}")
        
        
        toplu_ozet_metin = "".join(ara_ozetler)
        nihai_komut = f"{nihai_komut_yapisi}\n\nÖzetlenecek Materyal:\n\n{toplu_ozet_metin}"
        with st.spinner('⏳ Nihai Akıllı Not Raporu Oluşturuluyor...'):
            return ai_isteği_gonder(nihai_komut)
    else:
        
        komut_tek_parca = f"{nihai_komut_yapisi}\n\nÖzetlenecek Materyal:\n\n{tum_metin}"
        with st.spinner('⏳ Akıllı Not Oluşturuluyor...'):
            return ai_isteği_gonder(komut_tek_parca)



st.title("📚 Yapay Zeka Destekli PDF Not Özetleyici")
st.markdown("---")



with st.container():
    st.subheader("📁 PDF Dosyası Yükle")
    kalan_hak = MAX_FREE_SUMMARIES - st.session_state.quota_used
    if kalan_hak > 0:
        st.info(f"✨ **Ücretsiz deneme hakkınız var**: {kalan_hak} özet kaldı.")
    else:
        
        st.warning("⚠️ **Ücretsiz özetleme hakkınız kalmadı**. Premium'a geçin.")
        
       
        st.link_button(
            label="💎 Premium'a Geç (API Yükseltme)",
            url="https://cloud.google.com/billing",
            help="Google Cloud Faturalandırma sayfasına gider.",
            type="primary"
        )
        
    uploaded_file = st.file_uploader("PDF Dosyası:", type="pdf")


if uploaded_file:
   
    with st.container():
        st.subheader("🎯 Özetleme Kapsamı")
        col1, col2 = st.columns(2)
        
        
        try:
            uploaded_file.seek(0)
            pdf_bytes_for_count = uploaded_file.read()
            with fitz.open(stream=pdf_bytes_for_count, filetype="pdf") as belge:
                 toplam_sayfa = belge.page_count
        except Exception:
            toplam_sayfa = 1 
        with col1:
            start_page_input = st.number_input(
                f"Başlangıç Sayfası (Toplam: {toplam_sayfa}):", 
                min_value=1, 
                value=1, 
                max_value=toplam_sayfa,
                step=1
            )
        with col2:
            end_page_input = st.number_input(
                "Bitiş Sayfası (0 = tüm PDF):", 
                min_value=0, 
                value=0, 
                max_value=toplam_sayfa,
                step=1
            )


    
    if kalan_hak > 0:
        
        if st.button("🚀 Özeti Oluştur"):
            
            
            end_page = end_page_input if end_page_input > 0 else None
            tum_metin = pdf_metni_cikar(uploaded_file, start_page=start_page_input, end_page=end_page)
            
            if tum_metin:
                st.success(f"Metin başarıyla çıkarıldı. Toplam **{len(tum_metin):,}** karakter AI'a gönderiliyor.")
                ozet_notlar = tam_ozetleme_sureci(tum_metin)
                
                if ozet_notlar:
                    
                    
                    st.session_state.quota_used += 1
                    cookies[QUOTA_COOKIE_KEY] = str(st.session_state.quota_used)
                    cookies.save()
                    
                    
                    st.markdown("---")
                    st.markdown("## ✅ AKILLI NOT RAPORU")
                    st.markdown(ozet_notlar)
                    st.download_button("📄 Özeti İndir (ozet_notlar.md)", ozet_notlar, f"{uploaded_file.name}_ozet.md")