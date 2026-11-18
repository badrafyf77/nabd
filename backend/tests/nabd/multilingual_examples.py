"""Phase 4 equivalent examples. Held outside prompts; native review still pending."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Form:
    name: str
    language: str
    reply_language: str
    script: str
    knowledge: str
    allowed_return: str
    late_return: str
    refund: str
    delayed: str
    followup: str
    ambiguous: str

    @property
    def selection(self):
        return {"input_language": self.language, "reply_language": self.reply_language, "reply_script": self.script}

FORMS = [
    Form("english", "en", "en", "latin", "How much does delivery cost?",
         "Can I still return my Bluetooth speaker?", "Can I still return my trail running shoes?",
         "I want a refund for my Bluetooth speaker.", "My cooking pot SE-1061 is late.",
         "The same cooking pot still hasn't arrived. Please update that case.",
         "I want to return something I bought, but I haven't said which item yet."),
    Form("french", "fr", "fr", "latin", "Combien coûte la livraison ?",
         "Puis-je encore retourner mon enceinte Bluetooth ?", "Puis-je encore retourner mes chaussures de trail ?",
         "Je veux être remboursé pour mon enceinte Bluetooth.", "Ma cocotte SE-1061 est en retard.",
         "Cette même cocotte n'est toujours pas arrivée. Ajoutez cela au même dossier, s'il vous plaît.",
         "Je voudrais retourner un de mes achats, mais je n'ai pas encore précisé lequel."),
    Form("arabic", "ar", "ar", "arabic", "كم تبلغ تكلفة التوصيل؟",
         "هل ما زال بإمكاني إرجاع مكبر الصوت الذي يعمل بالبلوتوث؟", "هل ما زال بإمكاني إرجاع حذاء الجري على المسارات؟",
         "أريد استرداد ثمن مكبر الصوت الذي يعمل بالبلوتوث.", "تأخر وصول قدر الطبخ في الطلب SE-1061.",
         "قدر الطبخ نفسه لم يصل بعد. أضيفوا هذه المعلومة إلى الشكوى نفسها من فضلكم.",
         "أريد إرجاع شيء اشتريته، لكنني لم أحدد أي منتج بعد."),
    Form("darija_latin", "ary", "ary", "latin", "Bch7al livraison?",
         "Wach mazal nqder nrjje3 l'enceinte Bluetooth dyali?", "Wach mazal nqder nrjje3 sbat dyal trail li chrit?",
         "Bghit nrjje3 flous l'enceinte Bluetooth dyali.", "Lqedra dyal SE-1061 t3ettlat 3liya.",
         "Nefs lqedra mazal ma wslatch. Zid hadchi f nefs dossier 3afak.",
         "Bghit nrjje3 chi 7aja chrit, walakin mazal ma geltch likom achmen produit."),
    Form("darija_arabic", "ary", "ary", "arabic", "بشحال التوصيل؟",
         "واش مازال نقدر نرجع لونسَانت بلوتوث ديالي؟", "واش مازال نقدر نرجع صباط ديال الترايل اللي شريت؟",
         "بغيت نرجع فلوس لونسَانت بلوتوث ديالي.", "القدرة ديال SE-1061 تعطلات عليا.",
         "نفس القدرة مازال ما وصلاتش. زيد هادشي فنفس الملف عافاك.",
         "بغيت نرجع شي حاجة شريت، ولكن مازال ما قلتش ليكم آشمن منتوج."),
    Form("mixed", "mixed", "fr", "latin", "Bch7al la livraison, please ? Répondez en français.",
         "Wach je peux encore retourner mon enceinte Bluetooth ? Répondez en français.",
         "Wach je peux encore retourner mes chaussures de trail ? Répondez en français.",
         "Bghit un remboursement pour mon enceinte Bluetooth, please. Répondez en français.",
         "Lqedra SE-1061 est en retard, ما وصلاتش. Répondez en français.",
         "Nefs lqedra n'est toujours pas arrivée, زيد هادشي فنفس الملف. Répondez en français.",
         "Bghit retourner something I bought, mais je n'ai pas précisé lequel. Répondez en français."),
]
