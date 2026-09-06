const pptxgen = require('pptxgenjs');
const fs = require('fs');
const path = require('path');
const { warnIfSlideHasOverlaps, warnIfSlideElementsOutOfBounds } = require('/home/oai/skills/slides/pptxgenjs_helpers');

const OUT = '/mnt/data';
const APP = path.join(OUT, 'SIGNALYTH-app-v1.7');
fs.rmSync(APP, { recursive: true, force: true });
fs.mkdirSync(APP, { recursive: true });
const logoSrc = fs.existsSync(path.join(OUT, 'logo_sig_small.png')) ? path.join(OUT, 'logo_sig_small.png') : path.join(OUT, 'SIGNALYTH-app-v1.6/logo.png');
fs.copyFileSync(logoSrc, path.join(APP, 'logo.png'));
const logoPath = path.join(APP, 'logo.png');

const C = {
  bg: 'F8F5EF', ink: '171614', muted: '6F6A62', line: 'D9D1C6', paper: 'FFFFFF',
  sand: 'EFE7DC', sandstone: 'E3D6C5', terracotta: 'C66042', red: 'A9453F', redSoft: 'F1DCD7',
  green: '617B55', greenSoft: 'DDE9D6', blue: '445E72', blueSoft: 'DCE7EC', gold: 'B89045', goldSoft: 'EEE3C9',
  plum: '5E4C59', lavender: 'E7E0EA', charcoal: '2D2926', black: '111111', white: 'FFFFFF',
  clientAccent: 'D71920', agencyAccent: '222222'
};
const W = 13.333, H = 7.5;
let CURRENT_PPTX = null;
const LANG = {
  EN: {
    fullReport: 'FULL CLIENT PRESENTATION', report: 'Brand Intelligence Report', period: 'Greece · 01 Mar - 13 Jun 2024',
    prepared: 'Prepared by agency', powered: 'Powered by SIGNALYTH', clientLogo: 'CLIENT LOGO', agencyLogo: 'AGENCY LOGO',
    sample: 'Evidence-complete benchmark · illustrative figures from reference deck',
    cockpit: 'Executive intelligence cockpit', rep: 'Brand Reputation', sent: 'Sentiment', volume: 'Mentions', views: 'Views', followers: 'Followers',
    crisis: 'CRISIS WATCH', positive: 'POSITIVE MOMENTUM', opportunity: 'OPPORTUNITY SIGNAL', what: 'What happened', why: 'What drove it', proof: 'Evidence the client can see',
    recs: 'Decision recommendations', act: 'ACT NOW', fix: 'FIX', keep: 'KEEP', exploit: 'EXPLOIT', watch: 'WATCH',
    comments: 'Actual comments / mentions', score: 'Score', date: 'Date', type: 'Type', source: 'Source / author', text: 'Text',
    positives: 'Positive evidence wall', negatives: 'Negative evidence wall', media: 'Media evidence', people: 'People / persons evidence', emotions: 'Emotion intelligence',
    anger: 'Anger', disgust: 'Disgust', fear: 'Fear', joy: 'Joy', sadness: 'Sadness', surprise: 'Surprise',
    coverage: 'Completeness & trust gate', appendix: 'Analytical appendix', note: 'No crisis is declared without independent voices, duration, source spread and evidence.'
  },
  EL: {
    fullReport: 'ΠΛΗΡΗΣ ΠΑΡΟΥΣΙΑΣΗ ΠΕΛΑΤΗ', report: 'Brand Intelligence Report', period: 'Ελλάδα · 01 Μαρ - 13 Ιουν 2024',
    prepared: 'Prepared by agency', powered: 'Powered by SIGNALYTH', clientLogo: 'LOGO ΠΕΛΑΤΗ', agencyLogo: 'LOGO ΕΤΑΙΡΕΙΑΣ',
    sample: 'Evidence-complete benchmark · ενδεικτικά στοιχεία από το reference deck',
    cockpit: 'Executive intelligence cockpit', rep: 'Brand Reputation', sent: 'Sentiment', volume: 'Αναφορές', views: 'Views', followers: 'Followers',
    crisis: 'CRISIS WATCH', positive: 'ΘΕΤΙΚΗ ΔΥΝΑΜΙΚΗ', opportunity: 'OPPORTUNITY SIGNAL', what: 'Τι συνέβη', why: 'Τι το οδήγησε', proof: 'Evidence που βλέπει ο πελάτης',
    recs: 'Συστάσεις απόφασης', act: 'ACT NOW', fix: 'FIX', keep: 'KEEP', exploit: 'EXPLOIT', watch: 'WATCH',
    comments: 'Πραγματικά comments / αναφορές', score: 'Score', date: 'Ημερομηνία', type: 'Τύπος', source: 'Πηγή / author', text: 'Κείμενο',
    positives: 'Θετικό evidence wall', negatives: 'Αρνητικό evidence wall', media: 'Evidence από media', people: 'Evidence από πρόσωπα/users', emotions: 'Emotion intelligence',
    anger: 'Θυμός', disgust: 'Αηδία', fear: 'Φόβος', joy: 'Χαρά', sadness: 'Λύπη', surprise: 'Έκπληξη',
    coverage: 'Έλεγχος πληρότητας & εμπιστοσύνης', appendix: 'Αναλυτικό appendix', note: 'Δεν δηλώνεται κρίση χωρίς independent voices, διάρκεια, source spread και evidence.'
  }
};
const metrics = { sentiment: [53.16, 19.93, 26.91], mediaPerson: [68, 32], source: [42, 28, 15, 9, 6], emotion: [23.92, 10.3, 3.99, 9.63, 3.99, 17.61, 30.56] };
const days = ['10 Mar','17 Mar','24 Mar','31 Mar','07 Apr','14 Apr','21 Apr','28 Apr','05 May','12 May','19 May','26 May','02 Jun','09 Jun'];
const reputation = [5.8,6.4,6.2,5.7,5.9,6.1,4.0,1.0,2.7,4.2,5.2,4.7,3.5,6.7];
const negativeVol = [4,6,7,5,9,10,31,54,18,14,12,28,22,8];
const positiveVol = [12,15,24,21,19,31,42,60,27,22,25,19,30,45];
const heat = [1,1,2,1,3,2,4,5,3,2,2,4,3,1, 1,2,1,1,2,3,4,5,2,1,2,3,4,2, 1,1,1,2,3,4,5,4,2,1,1,2,3,4, 1,2,3,4,2,1,1,2,3,5,4,3,2,1];
const positiveEvidence = [
 ['+1.00','29/4','Person','Καλημέρα'], ['+1.00','29/4','Person','👍'], ['+1.00','29/4','Person','Εννοείται Κρουαζιέρα!!!!'], ['+0.95','29/4','Person','👍 κρουαζιέρα'],
 ['+0.90','24/3','Media','Στη Λαμία ο πρώτος μεγάλος τυχερός του Eurojackpot από Ελλάδα - κέρδισε 986.995 ευρώ'], ['+0.90','29/4','Person','Κρουαζιέρα καλή επιτυχία σε όλους σας ευχαριστούμε πολύ.'], ['+0.90','29/4','Person','GOOD LUCK WITH ALL MY HEART TO EVERYONE'], ['+0.90','21/4','Person','πρεπει να εισαι μοναδικος πηγαινε παιξε EUROJACKPOT'],
 ['+0.90','17/4','Person','115 μύρια το eurojackpot. Μου φτάνει. Σπίτι στην πλάκα, λοφτ στη ΝΥ...'], ['+0.90','29/4','Person','Θα βοηθήσω αρκετές οικογένειες που έχουν πραγματικά ανάγκη.'], ['+0.85','29/4','Person','Θα γύριζα τον κόσμο με ποδήλατο'], ['+0.85','29/4','Person','Μια μονοκατοικία!'],
 ['+0.80','24/3','Media','Eurojackpot: Αυτό είναι το κατάστημα ΟΠΑΠ στη Λαμία όπου παίχθηκε το χρυσό δελτίο'], ['+0.80','29/4','Media','Eurojackpot: Καλή επιτυχία Μαρίνα Σάττι! #ΟΠΑΠ #Eurojackpot'], ['+0.80','11/4','Media','Giga τζακ ποτ 86 εκατ. ευρώ στο Eurojackpot - μεγάλη κλήρωση'], ['+0.80','29/4','Person','Θα αγόραζα τον Απόλλων δοκιμίου!!!'], ['+0.80','29/4','Person','Θα βοηθυθυσα τους φτοχυς'], ['+0.80','29/4','Person','Κρουαζιέρα!'], ['+0.80','4/6','Person','Μπράβο να τα χάρη'], ['+0.80','7/6','Person','Όλη η Ευρώπη παίζει και βγαίνει Τζακ Ποτ'],
 ['+0.75','26/5','Person','Ιστορία τύχης με λαχείο και πραγματικό νικητή σε πρακτορείο'], ['+0.75','29/4','Person','Σιγά μην φάτε έχουνε γλαροσουπα ΑΧΑΧΑΧΑ'], ['+0.70','25/3','Media','Το μεγαλύτερο τζακ ποτ όλων των εποχών στην Ελλάδα'], ['+0.70','11/4','Media','Το Eurojackpot μοιράζει 86 εκατ. ευρώ απόψε στις 21:00'], ['+0.70','29/4','Person','Δεν μπορώ να αποφασίσω ποιο να διαλέξω'], ['+0.70','28/3','Person','Κύρ Θάνο πες σε παρακαλώ την αλήθεια. Σου έκατσε το eurojackpot;'], ['+0.65','29/4','Media','Χόρεψε το Zari παρέα με τη Μαρίνα Σάττι - Eurojackpot activation'], ['+0.60','30/4','Media','Χαμόγελα για 8 τυχερούς - κερδίζουν από 250 χιλιάδες ευρώ']
];
const negativeEvidence = [
 ['-0.90','29/4','Person','ΑΠΑΤΕΩΝΕΣΣ ΤΟΥ ΟΠΑΠ ΕΠΡΕΠΕ ΝΑ ΕΙΣΤΕ ΟΛΟΙ ΦΥΛΑΚΗ...'], ['-0.90','24/5','Person','Καθικια'], ['-0.80','29/4','Person','115 pige kai tosw Laos pezi qpo pantou kai den eskwse akoma koroidia...'], ['-0.80','1/4','Person','Η τακτική του gaslighting ανεψιού, τώρα και σε eurojackpot-έκδοση'],
 ['-0.80','20/4','Person','Μας τα έχετε πρήξει με το Eurojackpot... δεν κερδίζεις 120.000.000 έτσι για πλάκα'], ['-0.80','21/5','Person','Νόμιμοι κλέφτες...'], ['-0.80','28/5','Person','Δώστε κάνα φράγκο παραπάνω και όχι 0.5€ - 1€ - 2€'], ['-0.80','29/4','Person','Σε καμία περίπτωση δεν θα έχουμε ποτέ μα ποτέ'], ['-0.80','20/4','Person','Χθες οι Έλληνες παίξαμε μόνο στο Eurojackpot 1.751.455 ευρώ... φτώχεια και μιζέρια'], ['-0.70','10/6','Person','Εγώ τίποτα δεν παίζω ξανά'], ['-0.70','29/4','Person','Ούτε απ έξω θα περάσω!'], ['-0.70','21/4','Person','Δεν κερδίζεις έτσι εύκολα 120.000.000€ στο Eurojackpot...'], ['-0.70','29/4','Person','παει τρελλαθηκατε τελειως'], ['-0.70','11/6','Person','Πάλι πουστια'], ['-0.70','23/4','Person','Χάσαμε στο μπάσκετ, χάσαμε και τα εκατομμύρια του eurojackpot...'], ['-0.65','31/5','Person','Οι πίνακες νικητών που παρουσιάζουν άγνωστο αν είναι αληθινοί'], ['-0.60','21/4','Person','Λιγότερες πιθανότητες από το να κέρδισες το EuroJackpot. Πρωτάθλημα τέλος.'], ['-0.70','12/5','Person','Επαιξα eurojackpot. Πανάκριβη η μαλακία'], ['-0.70','27/5','Person','με 2.50 η στηλη αντιο απο μενα κ απο αλλους']
];
const mediaRows = [['1.00','SKAI.gr','High-impact TV/news visibility'], ['0.99','SKAI.gr','Largest jackpot of all time in Greece'], ['0.80','enikos.gr','Marina Satti sponsorship link'], ['0.79','Protothema.gr','Record prize coverage'], ['0.55','NEWS 24/7','OPAP stores / deposit reminder'], ['0.51','iefimerida','115M jackpot headline'], ['0.44','NewsIt','Repeated draw/winner coverage'], ['0.43','star.gr','Mainstream amplification'], ['0.38','newsbomb.gr','Lottery result coverage'], ['0.34','Zougla.gr','Result and winner updates'], ['0.31','FTHIS.GR','Eurovision party activation'], ['0.27','Eurovisionfun','Satti / Eurovision brand moment']];
const personRows = [['0.49','ΕΠΙΣΗΜΗ ΑΝΤΙΠΡΟΣΩΠΕΙΑ HOUNDA MOTOR','High reach anomaly, review required'], ['0.34','Σαν να ταν','Consumer culture reference'], ['0.27','Cristian_stel','Eurovision activation discussion'], ['0.26','PrincessTaiwan','Consumer response'], ['0.25','Jeanne Dark','Sponsorship discussion'], ['0.20','Άγγελος','Public reaction'], ['0.19','Sea Bass','Repeated consumer participation'], ['0.18','Chris Pro','Lottery criticism'], ['0.17','Manos Oikonomidis','Political/sport crossover'], ['0.16','Κοκοπούδρα','Ironic consumer voice'], ['0.14','Ξυπόλητος Πρίγκιψ','Humour/criticism'], ['0.12','Georgia','General reaction']];
const emotionRows = {
 Anger: negativeEvidence.slice(0,10),
 Disgust: [['0.80','2/6','Person','Ακόμα πιο απατεωνιά από το τζόκερ νέο φυτό άνθησε'], ['0.80','29/4','Person','δεν μας εμεινε σαλιο με αυτες τις συμμοριες'], ['0.80','11/6','Person','Πάλι πουστια'], ['0.70','27/3','Person','Είστε πολύ αστείοι όσοι στηρίζετε αυτό το αστείο'], ['0.70','15/4','Person','Παίξτε για να πάρουν κι άλλα ακίνητα...'], ['0.70','29/4','Person','ΑΠΑΤΕΩΝΕΣΣ ΤΟΥ ΟΠΑΠ...'], ['0.70','25/3','Person','να μυρίζεις σκορδαλιά, ντροπή...'], ['0.70','5/4','Person','δεν μας λέει που θα βρει να πληρώσει...'], ['0.70','30/5','Person','Μάλλον θα κερδίζει κάθε βδομάδα Eurojackpot :)'], ['0.70','22/5','Person','Απλά δεν παίζεις αφού είναι κλέφτες']],
 Fear: [['0.80','29/4','Person','Κρουαζιέρα με πλωτό σπίτι. Θα φοβόμουν να κάνω σκι στον Ατλαντικό'], ['0.40','22/5','Person','Προσωπικά πολύ σπάνια παίζω'], ['0.30','14/5','Person','Παιχνίδι πάντα με σημαδεμένη τράπουλα και δεν κερδίζει κανείς']],
 Joy: positiveEvidence.slice(0,14),
 Sadness: negativeEvidence.slice(8,19),
 Surprise: [['0.80','28/3','Person','Κύρ Θάνο πες την αλήθεια. Σου έκατσε το eurojackpot;'], ['0.80','29/4','Person','ΤΟΥΜΠΕΣ!!!!'], ['0.70','29/4','Person','Μια μονοκατοικία!'], ['0.70','26/5','Person','τι χίλια ευρώ ρε, ένα εκατομμύριο έχεις κερδίσει'], ['0.60','6/5','Person','ρε τι γίνεται στον κόσμο'], ['0.60','29/4','Person','πες μου και τα νούμερα του eurojackpot'], ['0.60','29/4','Person','Δεν μπορώ να αποφασίσω ποιο να διαλέξω'], ['0.55','24/3','Media','πρώτος μεγάλος τυχερός στην Ελλάδα']]
};
function addBg(slide){
  slide.background = { color: C.bg };
}
function footer(slide, lang, section=''){
  slide.addText(section,{x:0.45,y:7.05,w:4,h:0.18,fontFace:'Aptos',fontSize:6.5,color:C.muted,margin:0});
  slide.addText('SIGNALYTH',{x:11.4,y:7.03,w:1.45,h:0.2,fontSize:7.5,bold:true,color:C.muted,align:'right',margin:0});
}
function title(slide, t, sub=''){
  slide.addText(t,{x:0.55,y:0.45,w:8.2,h:0.36,fontFace:'Aptos Display',fontSize:21,bold:true,color:C.ink,margin:0,breakLine:false,fit:'shrink'});
  if(sub) slide.addText(sub,{x:0.57,y:0.88,w:8.6,h:0.25,fontFace:'Aptos',fontSize:8.8,color:C.muted,margin:0,fit:'shrink'});
}
function bigMetric(slide, x,y,w,h, label, value, sub, color=C.ink, fill=C.paper){
  slide.addShape(CURRENT_PPTX.ShapeType.roundRect,{x,y,w,h,rectRadius:0.08,fill:{color:fill},line:{color:C.line,transparency:40}});
  slide.addText(label,{x:x+0.18,y:y+0.16,w:w-0.36,h:0.22,fontSize:7.5,color:C.muted,bold:true,margin:0,fit:'shrink'});
  slide.addText(value,{x:x+0.18,y:y+0.44,w:w-0.36,h:0.53,fontSize:24,bold:true,color,margin:0,fit:'shrink'});
  if(sub) slide.addText(sub,{x:x+0.18,y:y+1.02,w:w-0.36,h:0.22,fontSize:7,color:C.muted,margin:0,fit:'shrink'});
}
function addChart(slide,type,data,opts){
  slide.addChart(type,data,{showLegend:false,showValue:false,showCategoryName:false,showTitle:false,chartColors:[C.green,C.gold,C.red,C.blue,C.plum,C.terracotta,C.muted],showLeaderLines:false,...opts});
}
function table(slide, x,y,w,h, rows, header, fontSize=6.2){
  const pptRows = [header.map(v=>({text:v,options:{bold:true,color:C.ink}})), ...rows.map(r=>r.map(v=>String(v)))];
  const colW = header.length===4 ? [0.75,0.85,1.05, Math.max(1,w-2.65)] : undefined;
  slide.addTable(pptRows,{x,y,w,h,colW,border:{type:'solid',color:C.line,pt:0.35},fill:{color:C.paper},fontFace:'Aptos',fontSize,color:C.ink,margin:0.035,breakLine:false,autoFit:false,fit:'shrink', valign:'mid', rowH: h/pptRows.length});
}
function addBrand(slide, lang, prominent=false){
  const L=LANG[lang];
  slide.addShape(CURRENT_PPTX.ShapeType.roundRect,{x:9.6,y:0.38,w:2.1,h:0.43,rectRadius:0.05,fill:{color:C.white},line:{color:C.clientAccent,pt:1}});
  slide.addText(L.clientLogo,{x:9.7,y:0.51,w:1.9,h:0.12,fontSize:6.5,bold:true,color:C.clientAccent,align:'center',margin:0});
  if(prominent){
    slide.addShape(CURRENT_PPTX.ShapeType.roundRect,{x:0.62,y:5.98,w:1.8,h:0.38,rectRadius:0.05,fill:{color:C.white},line:{color:C.line}});
    slide.addText(L.agencyLogo,{x:0.72,y:6.11,w:1.6,h:0.1,fontSize:5.8,color:C.muted,bold:true,align:'center',margin:0});
  }
}
function evidencePages(pptx, lang, titleText, rows, rowsPerPage, section){
  const L=LANG[lang];
  const pages=Math.ceil(rows.length/rowsPerPage);
  for(let p=0;p<pages;p++){
    const slide=pptx.addSlide(); addBg(slide); addBrand(slide,lang); title(slide, `${titleText} ${p+1}/${pages}`, L.comments);
    const chunk=rows.slice(p*rowsPerPage,(p+1)*rowsPerPage);
    table(slide,0.55,1.35,12.22,5.36,chunk, [L.score,L.date,L.type,L.text], 5.65);
    slide.addText('Evidence density: extensive · original wording preserved',{x:0.58,y:6.82,w:8,h:0.18,fontSize:6.5,color:C.muted,margin:0});
    footer(slide,lang,section);
  }
}
function createDeck(lang){
  const L=LANG[lang];
  const pptx=new pptxgen(); CURRENT_PPTX=pptx; pptx.layout='LAYOUT_WIDE'; pptx.author='SIGNALYTH'; pptx.company='SIGNALYTH'; pptx.subject='SIGNALYTH v1.7 WOW Presentation + Decision Intelligence'; pptx.title=`SIGNALYTH v1.7 ${lang}`; pptx.lang=lang==='EL'?'el-GR':'en-US'; pptx.theme={headFontFace:'Aptos Display',bodyFontFace:'Aptos',lang:pptx.lang}; pptx.defineLayout({name:'CUSTOM',width:W,height:H}); pptx.layout='CUSTOM';
  // 1 Cover
  let s=pptx.addSlide(); addBg(s);
  s.addImage({path:logoPath,x:0.57,y:0.56,w:1.45,h:0.45});
  s.addShape(pptx.ShapeType.roundRect,{x:8.85,y:0.70,w:2.65,h:0.62,rectRadius:0.08,fill:{color:C.white},line:{color:C.clientAccent,pt:1.3}});
  s.addText(L.clientLogo,{x:9.0,y:0.91,w:2.35,h:0.17,fontSize:9,bold:true,color:C.clientAccent,align:'center',margin:0});
  s.addText('EUROJACKPOT',{x:0.62,y:2.18,w:7.6,h:0.35,fontSize:20,bold:true,color:C.ink,margin:0});
  s.addText(L.report,{x:0.62,y:2.62,w:8.8,h:0.62,fontSize:34,bold:true,color:C.ink,margin:0,fit:'shrink'});
  s.addText(L.fullReport,{x:0.63,y:3.38,w:6.3,h:0.22,fontSize:9.2,bold:true,color:C.terracotta,margin:0});
  s.addText(L.period,{x:0.63,y:3.75,w:5,h:0.24,fontSize:11,color:C.muted,margin:0});
  s.addShape(pptx.ShapeType.roundRect,{x:0.63,y:6.19,w:2.0,h:0.39,rectRadius:0.04,fill:{color:C.white},line:{color:C.line}});
  s.addText(L.prepared,{x:0.78,y:6.31,w:1.7,h:0.11,fontSize:5.8,color:C.muted,align:'center',margin:0});
  s.addText(L.powered,{x:9.1,y:6.42,w:3.2,h:0.16,fontSize:7,color:C.muted,align:'right',margin:0});
  // 2 Branding system
  s=pptx.addSlide(); addBg(s); title(s, lang==='EL'?'Branding ανά πελάτη':'Client-specific branding', lang==='EL'?'Logo πελάτη, δικό σας logo και SIGNALYTH σε σωστή ιεραρχία':'Client logo, agency logo and SIGNALYTH in the correct hierarchy');
  addBrand(s,lang);
  const bcards = [[L.clientLogo, lang==='EL'?'Ανεβαίνει μία φορά στο Client Profile και εμφανίζεται στο cover / report context.':'Uploaded once in Client Profile and used automatically in cover / report context.', C.clientAccent],[L.agencyLogo, lang==='EL'?'Μένει διακριτικό: cover, closing, όχι σε κάθε slide.':'Kept discreet: cover and closing, not every slide.', C.agencyAccent],['SIGNALYTH', lang==='EL'?'Powered by, πολύ μικρό και premium.':'Powered by, very small and premium.', C.muted]];
  bcards.forEach((b,i)=>{let x=0.8+i*4.1; s.addShape(pptx.ShapeType.roundRect,{x,y:1.55,w:3.35,h:3.3,rectRadius:0.12,fill:{color:C.white},line:{color:C.line}}); s.addText(b[0],{x:x+0.25,y:1.95,w:2.85,h:0.28,fontSize:18,bold:true,color:b[2],align:'center',margin:0,fit:'shrink'}); s.addText(b[1],{x:x+0.35,y:2.75,w:2.65,h:1.0,fontSize:10,color:C.muted,align:'center',valign:'mid',margin:0.02,fit:'shrink'}); });
  s.addText(lang==='EL'?'Κανόνας: το client brand πρωταγωνιστεί. Το agency και το SIGNALYTH υπογράφουν, δεν κουράζουν.':'Rule: the client brand leads. Agency and SIGNALYTH sign the work, they do not dominate it.',{x:1.15,y:5.65,w:11,h:0.35,fontSize:15,bold:true,color:C.ink,align:'center',margin:0}); footer(s,lang,'BRANDING');
  // 3 Cockpit
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s,L.cockpit,L.sample);
  bigMetric(s,0.6,1.27,2.45,1.48,L.rep,'62/100','Bot/spam adjusted',C.green,C.white);
  bigMetric(s,3.25,1.27,2.45,1.48,L.volume,'632','301 analyzable texts',C.ink,C.white);
  bigMetric(s,5.9,1.27,2.45,1.48,L.views,'170K','audience signal',C.blue,C.white);
  bigMetric(s,8.55,1.27,2.45,1.48,L.followers,'10M','author reach',C.gold,C.white);
  s.addShape(pptx.ShapeType.roundRect,{x:0.6,y:3.15,w:5.5,h:3.38,rectRadius:0.12,fill:{color:C.white},line:{color:C.line}});
  s.addText(L.sent,{x:0.82,y:3.38,w:2,h:0.24,fontSize:12,bold:true,color:C.ink,margin:0});
  addChart(s,pptx.ChartType.doughnut,[{name:'Sentiment',labels:['Positive','Neutral','Negative'],values:metrics.sentiment}],{x:0.78,y:3.76,w:2.55,h:2.3,holeSize:62,showPercent:true,showLegend:true,legendPos:'r',dataLabelPosition:'bestFit',chartColors:[C.green,C.gold,C.red]});
  s.addShape(pptx.ShapeType.roundRect,{x:6.35,y:3.15,w:6.05,h:3.38,rectRadius:0.12,fill:{color:C.white},line:{color:C.line}});
  s.addText(lang==='EL'?'Reputation pulse':'Reputation pulse',{x:6.62,y:3.38,w:2.3,h:0.24,fontSize:12,bold:true,color:C.ink,margin:0});
  addChart(s,pptx.ChartType.line,[{name:'Brand Reputation',labels:days,values:reputation}],{x:6.62,y:3.83,w:5.3,h:2.25,showLegend:false,valAxis:{minVal:0,maxVal:7,majorUnit:1},catAxis:{labelRotation:45},lineSize:2,chartColors:[C.ink]});
  footer(s,lang,'EXECUTIVE');
  // 4 Event Radar
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s, lang==='EL'?'Event Radar: όχι απλό anomaly':'Event Radar: more than an anomaly', L.note);
  s.addShape(pptx.ShapeType.roundRect,{x:0.58,y:1.32,w:4.0,h:4.95,rectRadius:0.15,fill:{color:C.redSoft},line:{color:C.red,pt:1.0}});
  s.addText(L.crisis,{x:0.88,y:1.58,w:3.2,h:0.28,fontSize:18,bold:true,color:C.red,align:'center',margin:0});
  s.addText('3.4×',{x:0.95,y:2.25,w:3.0,h:0.8,fontSize:46,bold:true,color:C.red,align:'center',margin:0});
  s.addText(lang==='EL'?'negative conversation vs baseline':'negative conversation vs baseline',{x:0.93,y:3.15,w:3.0,h:0.24,fontSize:10,color:C.ink,align:'center',margin:0});
  ['Volume spike','Anger dominance','Independent voices','Media pickup','Authenticity check'].forEach((t,i)=>s.addText((lang==='EL'?['Όγκος','Θυμός','Independent voices','Media amplification','Authenticity'][i]:t),{x:1.0,y:3.7+i*0.32,w:2.75,h:0.16,fontSize:8.2,color:C.ink,margin:0}));
  s.addShape(pptx.ShapeType.roundRect,{x:4.95,y:1.32,w:7.72,h:4.95,rectRadius:0.15,fill:{color:C.white},line:{color:C.line}});
  addChart(s,pptx.ChartType.line,[{name:'Negative volume',labels:days,values:negativeVol},{name:'Positive volume',labels:days,values:positiveVol}],{x:5.2,y:1.74,w:7.05,h:2.3,showLegend:true,legendPos:'b',valAxis:{minVal:0},catAxis:{labelRotation:45},chartColors:[C.red,C.green]});
  s.addText(lang==='EL'?'Το peak δεν γίνεται αυτόματα crisis. Περνάει από guardrail: source spread, duration, independent voices, bot/coordination risk και evidence.':'A peak is not automatically a crisis. It passes guardrails: source spread, duration, independent voices, bot/coordination risk and evidence.',{x:5.35,y:4.52,w:6.72,h:0.58,fontSize:12,bold:true,color:C.ink,margin:0.02,fit:'shrink'});
  s.addText(lang==='EL'?'Classification: Crisis Watch · Confidence: High · Evidence: 94 independent mentions.':'Classification: Crisis Watch · Confidence: High · Evidence: 94 independent mentions.',{x:5.35,y:5.35,w:6.75,h:0.25,fontSize:10,color:C.red,bold:true,margin:0}); footer(s,lang,'EVENT DETECTION');
  // 5 Why / drivers
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s, L.why, lang==='EL'?'Συνδυασμός narratives, emotions, sources και evidence':'Combines narratives, emotions, sources and evidence');
  addChart(s,pptx.ChartType.bar,[{name:'Contribution',labels:['Rigged draw','Low odds','Ticket price','Big prize dream','Greek winner'],values:[-8.3,-5.1,-3.2,6.8,4.9]}],{x:0.7,y:1.35,w:5.9,h:4.8,barDir:'bar',showValue:true,catAxis:{labelFontSize:8},valAxis:{minVal:-10,maxVal:10},chartColors:[C.terracotta]});
  s.addShape(pptx.ShapeType.roundRect,{x:7.05,y:1.35,w:5.4,h:4.8,rectRadius:0.15,fill:{color:C.white},line:{color:C.line}});
  const bullets = lang==='EL'?['Conspiracy / rigged draw narrative drives the negative signal.','Μεγάλο έπαθλο και Έλληνας νικητής δημιουργούν positive momentum.','Η τιμή στήλης και οι πιθανότητες λειτουργούν ως recurring friction.','Media amplifies record jackpots; persons drive skepticism.']:['Conspiracy / rigged draw narrative drives the negative signal.','Large prize and Greek winner create positive momentum.','Ticket price and odds remain recurring friction.','Media amplifies record jackpots; persons drive skepticism.'];
  bullets.forEach((b,i)=>s.addText('• '+b,{x:7.4,y:1.85+i*0.67,w:4.6,h:0.36,fontSize:12,color:C.ink,margin:0,fit:'shrink'}));
  footer(s,lang,'INVESTIGATION');
  // 6 Recommendations
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s,L.recs, lang==='EL'?'Κάθε σύσταση συνδέεται με finding + evidence + timing':'Each recommendation is linked to finding + evidence + timing');
  const recs = [[L.act,'Address skepticism narrative with transparent mechanics explainer','High','Now',C.redSoft,C.red],[L.fix,'Clarify odds and ticket-value perception with plain-language content','High','This week',C.goldSoft,C.gold],[L.exploit,'Use Greek-winner stories and relatable prize scenarios in creative','Medium','Next campaign',C.greenSoft,C.green],[L.watch,'Monitor anger velocity and conspiracy language after jackpot peaks','High','Always-on',C.blueSoft,C.blue]];
  recs.forEach((r,i)=>{let x=0.65+(i%2)*6.1,y=1.35+Math.floor(i/2)*2.35; s.addShape(pptx.ShapeType.roundRect,{x,y,w:5.6,h:1.85,rectRadius:0.12,fill:{color:r[4]},line:{color:r[5],pt:0.8}}); s.addText(r[0],{x:x+0.24,y:y+0.19,w:1.25,h:0.2,fontSize:11,bold:true,color:r[5],margin:0}); s.addText(r[1],{x:x+0.24,y:y+0.52,w:4.98,h:0.55,fontSize:13,bold:true,color:C.ink,margin:0.02,fit:'shrink'}); s.addText(`Priority: ${r[2]} · Timing: ${r[3]} · Evidence-linked`,{x:x+0.24,y:y+1.30,w:4.9,h:0.16,fontSize:7.8,color:C.muted,margin:0}); }); footer(s,lang,'DECISION');
  // 7 Visual library
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s, lang==='EL'?'WOW visual system με κανόνες':'WOW visual system with rules', lang==='EL'?'Πολλά visuals, αλλά μόνο όταν στηρίζονται από δεδομένα':'Visual richness, but only when supported by data');
  addChart(s,pptx.ChartType.doughnut,[{name:'Media/Persons',labels:['Media','Persons'],values:metrics.mediaPerson}],{x:0.65,y:1.32,w:2.5,h:2.15,holeSize:62,showPercent:true,showLegend:true,chartColors:[C.blue,C.gold]});
  addChart(s,pptx.ChartType.bar,[{name:'Source mix',labels:['X','News','TikTok','IG','FB'],values:metrics.source}],{x:3.55,y:1.31,w:3.25,h:2.15,barDir:'bar',showValue:true,chartColors:[C.terracotta]});
  addChart(s,pptx.ChartType.radar,[{name:'Emotion',labels:['Joy','Anger','Fear','Surprise','Sadness'],values:[24,10,10,18,4]}],{x:7.2,y:1.18,w:2.5,h:2.35,showLegend:false,chartColors:[C.plum]});
  addChart(s,pptx.ChartType.bubble,[{name:'Narratives',labels:['Trust','Price','Prize','Winner'],values:[{x:8,y:22,size:18},{x:5,y:12,size:10},{x:14,y:20,size:25},{x:11,y:16,size:14}]}],{x:10.05,y:1.23,w:2.45,h:2.25,chartColors:[C.green],showLegend:false});
  s.addText(lang==='EL'?'Donuts · Stacked bars · Timelines · Heatmaps · Rankings · Bubble maps · Evidence walls · Crisis panels':'Donuts · Stacked bars · Timelines · Heatmaps · Rankings · Bubble maps · Evidence walls · Crisis panels',{x:0.8,y:4.2,w:11.7,h:0.35,fontSize:18,bold:true,color:C.ink,align:'center',margin:0,fit:'shrink'});
  s.addText(lang==='EL'?'Κάθε visual έχει data reason και presentation role. Δεν μπαίνει τίποτα για διακόσμηση.':'Every visual has a data reason and a presentation role. Nothing is inserted as decoration.',{x:1.7,y:4.93,w:10,h:0.25,fontSize:11,color:C.muted,align:'center',margin:0}); footer(s,lang,'VISUAL INTELLIGENCE');
  // 8 Heatmap
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s, lang==='EL'?'Calendar heatmap: πότε χτυπάει το θέμα':'Calendar heatmap: when the issue heats up', lang==='EL'?'Οι θερμές ημέρες οδηγούν investigation και recommendations':'Hot days trigger investigations and recommendations');
  const startX=1.15,startY=1.45,cell=0.37,gap=0.05;
  for(let i=0;i<heat.length;i++){let col=i%14,row=Math.floor(i/14); let val=heat[i]; let palette=[C.sand,C.goldSoft,C.gold,C.terracotta,C.red]; s.addShape(pptx.ShapeType.roundRect,{x:startX+col*(cell+gap),y:startY+row*(cell+gap),w:cell,h:cell,rectRadius:0.03,fill:{color:palette[val-1]},line:{color:C.bg}});}
  s.addText(lang==='EL'?'Στο full report, κάθε θερμό σημείο μπορεί να γίνει event slide με πραγματικά σχόλια και evidence.':'In Full mode, every hot spot can become an event slide with real comments and evidence.',{x:7.6,y:1.7,w:4.6,h:1.2,fontSize:20,bold:true,color:C.ink,margin:0.03,fit:'shrink'});
  bigMetric(s,7.6,3.5,2.1,1.25,lang==='EL'?'Hot days':'Hot days','9','above baseline',C.red,C.redSoft); bigMetric(s,10.05,3.5,2.1,1.25,lang==='EL'?'Events':'Events','3','investigated',C.blue,C.blueSoft); footer(s,lang,'HEATMAP');
  // 9 Source split
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s, lang==='EL'?'Source intelligence':'Source intelligence', lang==='EL'?'Το ίδιο sentiment δεν σημαίνει το ίδιο πράγμα σε κάθε πηγή':'The same sentiment does not mean the same thing on each source');
  addChart(s,pptx.ChartType.bar,[{name:'Positive',labels:['X','News','TikTok','IG','FB'],values:[41,58,49,52,33]},{name:'Neutral',labels:['X','News','TikTok','IG','FB'],values:[17,28,24,20,19]},{name:'Negative',labels:['X','News','TikTok','IG','FB'],values:[42,14,27,28,48]}],{x:0.8,y:1.36,w:6.2,h:4.6,barDir:'bar',showValue:true,showLegend:true,legendPos:'b',chartColors:[C.green,C.gold,C.red]});
  s.addText(lang==='EL'?'Facebook/X style consumer spaces may carry heavier complaint pressure, while News amplifies reach and credibility.':'Consumer spaces may carry heavier complaint pressure, while News amplifies reach and credibility.',{x:7.45,y:2.0,w:4.7,h:1.0,fontSize:19,bold:true,color:C.ink,margin:0.03,fit:'shrink'}); footer(s,lang,'SOURCE BREAKDOWN');
  // 10 Geographic only when real
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s, lang==='EL'?'Geography: χάρτης μόνο όταν υπάρχει σήμα':'Geography: maps only when data supports them', lang==='EL'?'Δεν εφευρίσκουμε τοποθεσία για να φαίνεται ωραίο':'We do not invent location to make a slide look pretty');
  const locs=[['Athens',42,3.9,2.4],['Thessaloniki',18,5.1,2.05],['Patras',9,4.3,4.05],['Heraklion',6,5.6,5.35],['Larissa',5,4.95,3.0]];
  locs.forEach((l,i)=>{s.addShape(pptx.ShapeType.ellipse,{x:0.95+l[2],y:1.35+l[3],w:0.25+l[1]/80,h:0.25+l[1]/80,fill:{color:i==0?C.red:C.terracotta,transparency:10},line:{color:C.white,transparency:100}});});
  s.addText('GREECE MARKET SIGNAL',{x:1.42,y:1.67,w:4.1,h:0.24,fontSize:12,bold:true,color:C.muted,align:'center',margin:0});
  table(s,7.0,1.45,4.9,3.25,locs.map(l=>[l[0],l[1],l[1]>20?'High':'Medium']),['Area','Mentions','Intensity'],7.0);
  s.addText(lang==='EL'?'Αν δεν υπάρχει reliable geo metadata, ο χάρτης αντικαθίσταται από market relevance warning.':'If reliable geo metadata does not exist, the map is replaced by a market relevance warning.',{x:7.15,y:5.15,w:4.6,h:0.48,fontSize:12,bold:true,color:C.ink,margin:0.02,fit:'shrink'}); footer(s,lang,'GEOGRAPHY');
  // 11 Comment wall teaser
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s, L.proof, lang==='EL'?'Ο πελάτης βλέπει τα ίδια τα σχόλια, όχι μόνο συμπέρασμα':'The client sees the actual comments, not only the conclusion');
  const quotes = negativeEvidence.slice(0,4).concat(positiveEvidence.slice(0,4));
  quotes.forEach((q,i)=>{let x=0.75+(i%2)*6.0,y=1.25+Math.floor(i/2)*1.38; let neg=q[0].startsWith('-'); s.addShape(pptx.ShapeType.roundRect,{x,y,w:5.35,h:1.05,rectRadius:0.08,fill:{color:neg?C.redSoft:C.greenSoft},line:{color:neg?C.red:C.green,pt:0.5}}); s.addText(`${q[0]} · ${q[1]} · ${q[2]}`,{x:x+0.18,y:y+0.16,w:4.95,h:0.13,fontSize:6.5,bold:true,color:neg?C.red:C.green,margin:0}); s.addText(q[3],{x:x+0.18,y:y+0.39,w:4.9,h:0.42,fontSize:9.8,color:C.ink,margin:0.01,fit:'shrink'}); }); footer(s,lang,'EVIDENCE WALL');
  // 12-13 positive evidence
  evidencePages(pptx,lang,L.positives,positiveEvidence,10,'APPENDIX · POSITIVE');
  evidencePages(pptx,lang,L.negatives,negativeEvidence,10,'APPENDIX · NEGATIVE');
  // Emotion overview
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s,L.emotions, lang==='EL'?'Distribution + trend + evidence ανά emotion':'Distribution + trend + evidence per emotion');
  addChart(s,pptx.ChartType.doughnut,[{name:'Emotions',labels:['Joy','Anger','Disgust','Fear','Sadness','Surprise','Neutral'],values:metrics.emotion}],{x:0.7,y:1.25,w:3.6,h:3.3,holeSize:60,showPercent:true,showLegend:true,legendPos:'r',chartColors:[C.green,C.red,C.plum,C.blue,C.gold,C.terracotta,C.muted]});
  addChart(s,pptx.ChartType.line,[{name:'Anger',labels:days,values:[0.1,0.2,0.15,0.2,0.3,0.35,0.85,0.9,0.4,0.25,0.3,0.6,0.5,0.2]},{name:'Joy',labels:days,values:[0.4,0.55,0.65,0.75,0.62,0.8,0.7,0.9,0.5,0.45,0.6,0.55,0.7,0.8]}],{x:5.0,y:1.35,w:7.2,h:3.1,showLegend:true,legendPos:'b',catAxis:{labelRotation:45},chartColors:[C.red,C.green]});
  s.addText(lang==='EL'?'Σε Full mode, κάθε applicable emotion έχει δικά του comments. Αν η βάση είναι μικρή, εμφανίζεται scarcity warning αντί για τεχνητά rows.':'In Full mode, every applicable emotion has its own comments. If evidence is scarce, a warning appears instead of artificial rows.',{x:1.4,y:5.35,w:10.5,h:0.48,fontSize:14,bold:true,color:C.ink,align:'center',margin:0.02}); footer(s,lang,'EMOTIONS');
  Object.entries(emotionRows).forEach(([em,rows])=>evidencePages(pptx,lang,(L[em.toLowerCase()]||em)+' · '+L.comments,rows,8,'APPENDIX · EMOTION'));
  // Media evidence / rankings
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s,L.media, lang==='EL'?'Headlines, scores, publishers και story clusters':'Headlines, scores, publishers and story clusters');
  table(s,0.65,1.28,12.05,5.5,mediaRows.map(r=>[r[0],'News',r[1],r[2]]),[L.score,'Type',L.source,L.text],5.7); footer(s,lang,'MEDIA');
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s,lang==='EL'?'Media influence ranking':'Media influence ranking',lang==='EL'?'Impact δεν είναι απλό volume':'Impact is not just volume');
  addChart(s,pptx.ChartType.bar,[{name:'Impact',labels:mediaRows.map(r=>r[1]).slice(0,10),values:mediaRows.map(r=>parseFloat(r[0])).slice(0,10)}],{x:0.75,y:1.35,w:6.1,h:4.9,barDir:'bar',showValue:true,chartColors:[C.blue]});
  table(s,7.2,1.35,4.9,4.9,mediaRows.slice(0,10).map(r=>[r[1],r[0]]),[L.source,'Impact'],7.2); footer(s,lang,'MEDIA RANKING');
  // People
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s,L.people, lang==='EL'?'Top persons/users και γιατί μετράνε':'Top persons/users and why they matter');
  table(s,0.7,1.3,11.9,5.25,personRows.map(r=>[r[0],'Person',r[1],r[2]]),['Impact','Type',L.source,L.text],6.0); footer(s,lang,'PERSONS');
  // 29 completeness gate
  s=pptx.addSlide(); addBg(s); addBrand(s,lang); title(s,L.coverage, lang==='EL'?'Κανένα applicable στοιχείο δεν εξαφανίζεται σιωπηλά':'No applicable element disappears silently');
  const gates=[['25 indicator families','MAIN / APPENDIX / N.A. + reason'],['Evidence density','Minimum rows or scarcity warning'],['Comments visibility','Real text + date + source + score'],['Crisis guardrail','No claim without evidence'],['Bilingual QA','EN / EL titles, labels, explanations'],['Branding QA','Client logo + agency logo + powered by hierarchy']];
  gates.forEach((g,i)=>{let x=0.8+(i%2)*5.95,y=1.35+Math.floor(i/2)*1.45; s.addShape(pptx.ShapeType.roundRect,{x,y,w:5.35,h:0.98,rectRadius:0.08,fill:{color:C.white},line:{color:C.line}}); s.addText(g[0],{x:x+0.18,y:y+0.18,w:4.9,h:0.18,fontSize:10.5,bold:true,color:C.ink,margin:0}); s.addText(g[1],{x:x+0.18,y:y+0.51,w:4.9,h:0.16,fontSize:7.5,color:C.muted,margin:0}); }); footer(s,lang,'QUALITY GATE');
  // 30 closing
  s=pptx.addSlide(); addBg(s); addBrand(s,lang,true); s.addImage({path:logoPath,x:0.58,y:0.58,w:1.45,h:0.45});
  s.addText(lang==='EL'?'Τι πρέπει να γίνει τώρα':'What to do next',{x:0.75,y:1.25,w:8,h:0.54,fontSize:34,bold:true,color:C.ink,margin:0});
  s.addText(lang==='EL'?'1. Ενεργοποίηση live pilot με πραγματικά credentials\n2. Vodafone Internet Greece ως stress case\n3. Έλεγχος τελικού PPTX, internal brief και evidence export\n4. Approval για production-grade reporting':'1. Run a live pilot with real credentials\n2. Use Vodafone Internet Greece as the stress case\n3. QA final PPTX, internal brief and evidence export\n4. Approve for production-grade reporting',{x:0.85,y:2.25,w:8.4,h:2.1,fontSize:17,color:C.ink,breakLine:false,margin:0.02,fit:'shrink'});
  s.addText(L.powered,{x:9.1,y:6.35,w:3.0,h:0.18,fontSize:8,color:C.muted,align:'right',margin:0});
  pptx._slides.forEach(sl=>{ warnIfSlideHasOverlaps(sl,pptx,{ignoreLines:true,ignoreDecorativeShapes:true}); warnIfSlideElementsOutOfBounds(sl,pptx); });
  return pptx;
}
async function main(){
  const en=createDeck('EN'); await en.writeFile({fileName:path.join(OUT,'SIGNALYTH-Step8-WOW-Decision-v1.7-EN.pptx')});
  const el=createDeck('EL'); await el.writeFile({fileName:path.join(OUT,'SIGNALYTH-Step8-WOW-Decision-v1.7-EL.pptx')});
  const contract={
    version:'1.7', scope:'STEP 8.3 - WOW Presentation + Decision Intelligence + Client Branding',
    builds_on:'v1.6 Evidence-Complete Presentation Engine',
    presentation_depth_modes:['Executive','Standard','Full'],
    branding:{client_profiles:true,client_logo_upload:true,agency_logo_setting:true,powered_by_signalyth_discreet:true,brand_accent_auto_detect_or_manual:true,cover_and_closing_logo_rule:true},
    visual_system:{native_editable_pptx:true,available_visuals:['donut','pie','stacked_bar','timeline','calendar_heatmap','rankings','bubble_matrix','geographic_intensity_when_supported','evidence_wall','crisis_panel','recommendation_matrix'],no_decoration_without_data_reason:true},
    event_detection:{negative:['Emerging Negative Signal','Crisis Watch','Sustained Reputation Pressure'],positive:['Positive Momentum','Opportunity Signal','Advocacy Spike'],guardrails:['volume_shift','sentiment_shift','emotion_shift','velocity','independent_voices','source_diversity','media_amplification','authenticity_risk','duration','evidence_count'],causality_guard:'Association is never presented as proven causation'},
    recommendations:{generic_recommendations_forbidden:true,fields:['finding_id','evidence_ids','action','priority','timing','owner_hint','monitor_metrics','confidence'],buckets:['ACT NOW','FIX','KEEP','EXPLOIT','WATCH']},
    completeness:{must_evaluate_25_indicator_families:true,must_place_applicable_indicators_in_main_or_appendix:true,must_include_evidence_density_gate:true,bilingual_EN_EL_QA:true},
    live_boundary:'Controlled benchmark only; final proof requires one live end-to-end run through Apify/OpenAI.'
  };
  fs.writeFileSync(path.join(APP,'presentation_wow_contract.v1.7.json'),JSON.stringify(contract,null,2));
  fs.writeFileSync(path.join(APP,'README.md'),`# SIGNALYTH v1.7\n\nStep 8.3 adds Client WOW + Decision Intelligence on top of v1.6.\n\n## Added\n\n- Client Profiles with client logo upload and brand accent.\n- Agency branding settings for your company logo.\n- Discreet Powered by SIGNALYTH treatment.\n- WOW Visual Intelligence: donuts, timelines, heatmaps, rankings, bubble/matrix logic, maps only when geographic evidence exists.\n- Event / Crisis / Opportunity detection contract.\n- Evidence-linked recommendation engine: ACT NOW / FIX / KEEP / EXPLOIT / WATCH.\n- Bilingual EN/EL report rendering.\n\n## Boundary\n\nNo paid Apify or OpenAI calls were made. This is the controlled presentation/decision-intelligence benchmark before live pilot.\n`);
  const html=`<!doctype html><html><head><meta charset="utf-8"><title>SIGNALYTH v1.7</title><style>body{margin:0;background:#f8f5ef;color:#171614;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}main{max-width:1120px;margin:0 auto;padding:54px 34px}.logo{width:140px}.hero{display:grid;grid-template-columns:1.2fr .8fr;gap:26px;align-items:stretch}h1{font-size:60px;letter-spacing:-.05em;line-height:.94;margin:46px 0 18px}.sub{font-size:19px;color:#6f6a62;max-width:740px}.card{background:white;border:1px solid #ded6ca;border-radius:28px;padding:26px;box-shadow:0 20px 60px rgba(70,55,35,.06)}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:34px}.k{font-size:12px;color:#6f6a62;text-transform:uppercase;letter-spacing:.08em}.big{font-size:28px;font-weight:750;margin-top:8px}.pill{display:inline-block;border-radius:999px;padding:8px 12px;background:#efe7dc;margin:5px 6px 0 0}.flow{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:28px}.flow div{background:#fff;border:1px solid #ded6ca;border-radius:22px;padding:18px;min-height:90px}@media(max-width:900px){.hero,.grid,.flow{grid-template-columns:1fr}h1{font-size:42px}}</style></head><body><main><img src="SIGNALYTH-app-v1.7/logo.png" class="logo"><section class="hero"><div><h1>WOW Presentation + Decision Intelligence</h1><p class="sub">v1.7 upgrades SIGNALYTH from evidence-complete reporting to client-ready visual storytelling: striking but data-grounded charts, event/crisis/opportunity detection, client branding and evidence-linked recommendations.</p><div><span class="pill">Client logo upload</span><span class="pill">Agency branding</span><span class="pill">EN / ΕΛ reports</span><span class="pill">Native editable PPTX</span></div></div><div class="card"><div class="k">New quality rule</div><div class="big">Jaw-dropping, never misleading.</div><p class="sub" style="font-size:15px">Every visual needs a data reason. Every recommendation needs evidence. Every crisis label needs guardrails.</p></div></section><section class="grid"><div class="card"><div class="k">Visuals</div><div class="big">WOW</div></div><div class="card"><div class="k">Events</div><div class="big">Crisis / Opportunity</div></div><div class="card"><div class="k">Actions</div><div class="big">ACT NOW</div></div><div class="card"><div class="k">Branding</div><div class="big">Client-first</div></div></section><section class="flow"><div><b>SEE IT</b><br><br>Donuts, timelines, heatmaps, rankings, matrices and evidence walls.</div><div><b>UNDERSTAND IT</b><br><br>Automatic investigations explain what changed and why it matters.</div><div><b>TRUST IT</b><br><br>Comments, sources, dates, scores and quality gates remain visible.</div><div><b>ACT ON IT</b><br><br>Recommendations are linked to evidence and monitor metrics.</div><div><b>BRAND IT</b><br><br>Client logo leads; agency and SIGNALYTH are discreet.</div></section></main></body></html>`;
  fs.writeFileSync(path.join(OUT,'SIGNALYTH-foundation-v1.7.html'),html);
}
main().catch(e=>{console.error(e); process.exit(1);});
