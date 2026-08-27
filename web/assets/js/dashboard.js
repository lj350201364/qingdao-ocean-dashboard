// Polyfills for Android 4.4.2 (Chrome 30)
if (!String.prototype.includes) { String.prototype.includes = function(s) { return this.indexOf(s) >= 0; }; }
if (!String.prototype.padStart) { String.prototype.padStart = function(len, ch) { var s = String(this); while (s.length < len) s = (ch || "0") + s; return s; }; }
var tideRawData=null, tideChart=null, lastChartRaw=null, lastChartSite=null, lastChartPoints=[], lastTideList=[], resizeTimer=null, lastTideRising=null, soundEnabled=false, audioCtx=null, selectedDayOffset=0, tomorrowTideList=[], tomorrowTideReady=false;
var $=function(id){return document.getElementById(id);};
function setText(id,text){var el=$(id); if(el) el.textContent=(text===null||text===undefined||text==="")?"--":text;}
function setTextWithUnit(id,val,unit){var el=$(id); if(el){var v=(val===null||val===undefined||val==="")?"--":val; el.innerHTML=v+(unit?'<small>'+unit+'</small>':'');}}
function lunarText(d){
  try{
    if(typeof Intl==="undefined"||!Intl.DateTimeFormat)return "农历 --";
    function zhNum(n){
      var digit=["零","一","二","三","四","五","六","七","八","九"];
      n=parseInt(n,10);
      if(isNaN(n))return "";
      if(n<=10)return n===10?"十":digit[n];
      if(n<20)return "十"+digit[n%10];
      if(n<100){
        var ten=Math.floor(n/10), one=n%10;
        return digit[ten]+"十"+(one?digit[one]:"");
      }
      return String(n).replace(/\d/g,function(x){return digit[parseInt(x,10)];});
    }
    function lunarDayText(n){
      var names=["初一","初二","初三","初四","初五","初六","初七","初八","初九","初十","十一","十二","十三","十四","十五","十六","十七","十八","十九","二十","廿一","廿二","廿三","廿四","廿五","廿六","廿七","廿八","廿九","三十"];
      return names[n-1]||zhNum(n);
    }
    var fmt=null;
    try{
      fmt=new Intl.DateTimeFormat("zh-CN-u-ca-chinese",{month:"long",day:"numeric"});
    }catch(e1){
      fmt=new Intl.DateTimeFormat("zh-u-ca-chinese",{month:"long",day:"numeric"});
    }
    if(!fmt)return "农历 --";
    var text="";
    if(typeof fmt.formatToParts==="function"){
      var parts=fmt.formatToParts(d),month="",day="";
      for(var i=0;i<parts.length;i++){
        if(parts[i].type==="month")month=parts[i].value;
        if(parts[i].type==="day")day=parts[i].value;
      }
      text=(month||"")+(day||"");
    }
    if(!text)text=fmt.format(d);
    text=String(text||"").replace(/\s+/g,"");
    if(!text||text==="InvalidDate")return "农历 --";
    text=text.replace(/(\d+)月/g,function(_,n){
      var map={1:"正",11:"冬",12:"腊"};
      n=parseInt(n,10);
      return (map[n]||zhNum(n))+"月";
    });
    text=text.replace(/(\d+)日?/g,function(_,n){ return lunarDayText(parseInt(n,10)); });
    return "农历 "+text;
  }catch(e){
    return "农历 --";
  }
}
function nowParts(){
  var d=new Date();
  var week=["星期日","星期一","星期二","星期三","星期四","星期五","星期六"][d.getDay()];
  var lunar=lunarText(d);
  return {
    time: String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0")+":"+String(d.getSeconds()).padStart(2,"0"),
    date: d.getFullYear()+"年"+String(d.getMonth()+1).padStart(2,"0")+"月"+String(d.getDate()).padStart(2,"0")+"日 "+week+" · "+lunar,
    dateOnly: d.getFullYear()+"年"+String(d.getMonth()+1).padStart(2,"0")+"月"+String(d.getDate()).padStart(2,"0")+"日 "+week,
    lunar: lunar
  };
}
function updateClock(){var p=nowParts();setText("nowTime",p.time);setText("dateText",p.date);setText("nowTimeBig",p.time);setText("dateTextBig",p.dateOnly);setText("lunarTextBig",p.lunar);}
function selectedDate(){
  var d=new Date();
  d.setDate(d.getDate()+selectedDayOffset);
  return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
}
function selectedDayLabel(){
  return selectedDayOffset===0?"今日":"明日";
}
function apiUrl(path){
  return path+"?date="+encodeURIComponent(selectedDate());
}
function updateDayButtons(){
  var today=$("todayBtn"), tomorrow=$("tomorrowBtn");
  if(today){if(selectedDayOffset===0){today.classList.add("active");}else{today.classList.remove("active");}today.setAttribute("aria-pressed",selectedDayOffset===0?"true":"false");}
  if(tomorrow){if(selectedDayOffset===1){tomorrow.classList.add("active");}else{tomorrow.classList.remove("active");}tomorrow.setAttribute("aria-pressed",selectedDayOffset===1?"true":"false");}
  setText("chartTitle","青岛"+selectedDayLabel()+"潮汐曲线");
}
function switchForecastDay(offset){
  selectedDayOffset=offset;
  lastTideRising=null;
  var els=document.querySelectorAll(".module-unavailable");
  for(var i=0;i<els.length;i++){els[i].parentNode.removeChild(els[i]);}
  updateDayButtons();
  refreshAllData();
}
function fetchJSON(url,timeout,cb){
  timeout=timeout||20000;
  var sep=url.indexOf("?")>=0?"&":"?";
  var xhr=new XMLHttpRequest();
  xhr.open("GET",url+sep+"_t="+Date.now(),true);
  xhr.timeout=timeout;
  xhr.onload=function(){
    if(xhr.status>=200&&xhr.status<300){
      try{cb(null,JSON.parse(xhr.responseText));}catch(e){cb(e);}
    }else{cb(new Error("HTTP "+xhr.status));}
  };
  xhr.onerror=function(){cb(new Error("Network error"));};
  xhr.ontimeout=function(){cb(new Error("Timeout"));};
  xhr.send();
}
function reloadTyphoonFrame(){
  var f=$("typhoonFrame"); if(f) f.src="https://www.bhyb.org.cn/typhoon/?t="+Date.now();
}
function formatHHMM(s){
  if(!s||s==="-")return "--";
  if(String(s).indexOf(":")>=0){var parts=String(s).split(":");var h=parts[0],m=parts[1];return String(parseInt(h,10)).padStart(2,"0")+":"+String(parseInt(m,10)).padStart(2,"0");}
  var raw=String(s).replace(":","").padStart(4,"0"); return raw.slice(0,2)+":"+raw.slice(2,4);
}
function timeToMin(s){
  if(!s||s==="-")return null; var t=formatHHMM(s); var parts=t.split(":"); var h=Number(parts[0]),m=Number(parts[1]);
  if(isNaN(h)||isNaN(m))return null; return h*60+m;
}
function parseTidePointTime(s){
  if(s===null||s===undefined||s==="")return null; var text=String(s);
  if(text.indexOf(":")>=0){var parts=text.split(":");var hh=parts[0],mm=parts[1];var h=parseInt(hh,10),m=parseInt(mm,10);if(isNaN(h)||isNaN(m))return null;return {label:String(h).padStart(2,"0")+":"+String(m).padStart(2,"0"),minute:h*60+m};}
  var h=parseInt(text,10); if(isNaN(h))return null; return {label:String(h).padStart(2,"0")+":00",minute:h*60};
}
function todayKey(){return selectedDate();}
function normalizeDateKey(s){
  if(!s)return ""; var datePart=String(s).split(" ")[0].replace(/-/g,"/"); var p=datePart.split("/");
  if(p.length<3)return ""; return p[0]+"-"+String(p[1]).padStart(2,"0")+"-"+String(p[2]).padStart(2,"0");
}
function windLevelText(speedText){
  var speed=parseFloat(String(speedText||"").replace(/[^\d.]/g,""));
  if(isNaN(speed))return "--";
  var levels=[
    [1,"0级 静风"],[5,"1级 软风"],[11,"2级 轻风"],[19,"3级 微风"],
    [28,"4级 和风"],[38,"5级 清风"],[49,"6级 强风"],[61,"7级 疾风"],
    [74,"8级 大风"],[88,"9级 烈风"],[102,"10级 狂风"],[117,"11级 暴风"],[Infinity,"12级 飓风"]
  ];
  var found=null;
  for(var i=0;i<levels.length;i++){if(speed<=levels[i][0]){found=levels[i];break;}}
  return (found||levels[levels.length-1])[1];
}
function parseWindDegree(degreeText){
  var degree=parseFloat(String(degreeText||"").replace(/[^\d.]/g,""));
  if(isNaN(degree))return null;
  return ((degree%360)+360)%360;
}
function waveLevelText(waveText){
  var wave=parseFloat(String(waveText||"").replace(/[^\d.]/g,""));
  if(isNaN(wave))return "--";
  if(wave<0.3)return "平静";
  if(wave<0.8)return "轻浪";
  if(wave<1.5)return "中浪";
  if(wave<2.5)return "大浪";
  return "风浪大";
}
function waterComfortText(tempText){
  var temp=parseFloat(String(tempText||"").replace(/[^\d.]/g,""));
  if(isNaN(temp))return "--";
  if(temp<18)return "偏冷";
  if(temp<22)return "较凉";
  if(temp<=28)return "舒适";
  if(temp<=31)return "偏暖";
  return "较热";
}
function seaRiskText(waveText, swimTip){
  var wave=parseFloat(String(waveText||"").replace(/[^\d.]/g,""));
  var tip=String(swimTip||"");
  if(tip.indexOf("不")>=0||tip.indexOf("禁")>=0||tip.indexOf("危险")>=0)return "谨慎下海";
  if(!isNaN(wave)&&wave>=1.5)return "风浪偏大";
  if(!isNaN(wave)&&wave>=0.8)return "注意浪涌";
  if(tip.indexOf("适宜")>=0)return "风险较低";
  return tip&&tip!=="--"?tip:"关注海况";
}
function weatherIconClass(weatherText){
  var text=String(weatherText||"");
  if(text.indexOf("雷")>=0)return "storm";
  if(text.indexOf("雨")>=0)return "rainy";
  if(text.indexOf("雪")>=0)return "snow";
  if(text.indexOf("雾")>=0||text.indexOf("霾")>=0)return "fog";
  if(text.indexOf("晴")>=0)return "sunny";
  return "cloudy";
}
function renderWeather(obj,updateTime){
  setText("weatherTime","更新 "+(updateTime||"--")); setText("weatherText",obj&&obj.weather);
  var icon=$("weatherIcon"); if(icon) icon.className="weather-icon "+weatherIconClass(obj&&obj.weather);
  setText("tempRange",obj&&obj.temperature_range);
  setText("humidity",obj&&obj.humidity);
  var windText=(obj&&obj.wind_direction?obj.wind_direction:"--")+" "+(obj?windLevelText(obj.wind_speed):"--");
  setText("windDirection",windText);
  var windDegree=parseWindDegree(obj&&obj.wind_direction_degree);
  var needle=$("windNeedle"); if(needle&&windDegree!==null) needle.style.transform="rotate("+windDegree+"deg)";
}
// 计算紫外线指数（基于天气、季节和时间估算）
function estimateUVIndex(){
  var now=new Date();
  var hour=now.getHours();
  var month=now.getMonth()+1;
  var weatherText=($("weatherText")&&$("weatherText").textContent)||"";
  var base=3;
  // 季节影响（夏季最高）
  if(month>=6&&month<=8)base=8;
  else if(month===5||month===9)base=6;
  else if(month===4||month===10)base=4;
  else if(month===3||month===11)base=2;
  else base=1;
  // 天气影响
  if(weatherText.indexOf("晴")>=0)base*=1.2;
  else if(weatherText.indexOf("多云")>=0)base*=0.8;
  else if(weatherText.indexOf("阴")>=0)base*=0.5;
  else if(weatherText.indexOf("雨")>=0||weatherText.indexOf("雪")>=0)base*=0.2;
  // 时间影响（中午最强）
  var hourFactor=1;
  if(hour>=10&&hour<=14)hourFactor=1.2;
  else if(hour>=8&&hour<10)hourFactor=0.8;
  else if(hour>14&&hour<=16)hourFactor=0.8;
  else if(hour>=6&&hour<8)hourFactor=0.4;
  else if(hour>16&&hour<=18)hourFactor=0.4;
  else hourFactor=0.1;
  var uv=Math.round(base*hourFactor*10)/10;
  return Math.max(0,Math.min(11,uv));
}
// 紫外线等级描述
function uvLevelText(uv){
  if(uv<=2)return "弱";
  if(uv<=4)return "较弱";
  if(uv<=6)return "中等";
  if(uv<=8)return "较强";
  if(uv<=10)return "强";
  return "很强";
}
function renderWave(obj,updateTime){
  if(obj&&obj.water_temp){setText("beachWaterTemp",obj.water_temp);}
  if(!obj||!obj.water_temp){setText("beachWaterTemp","--");}
}
function tideCurrentStatus(){
  var now=new Date();
  var hm=now.getHours()*60+now.getMinutes();
  var list=lastTideList||[];
  if(!list.length)return "";
  // 找当前之后的第一个潮
  var next=null, prev=null;
  for(var i=0;i<list.length;i++){
    var m=timeToMin(list[i].t);
    if(m===null)continue;
    if(m>hm){next=list[i];break;}
    prev=list[i];
  }
  if(!next&&prev)next=list[0];
  if(!next)return "";
  return (next.type==="满潮"?"涨潮中→":"落潮中→")+next.type;
}
function renderTide(res,upTime){
  tideRawData=res; setText("tideUpdate","更新 "+(upTime||"--")); setText("globalUpdate","数据更新 "+(upTime||"--"));
  if(!res||!res.data||!Array.isArray(res.data.rows)){ return; }
  var item=res.data.rows[0]; if(!item){ return;}
  var list=[
    {t:item.FIRSTHIGHTIME,l:item.FIRSTHIGHLEVEL,type:"满潮",cls:"high"},
    {t:item.SECONDHIGHTIME,l:item.SECONDHEIGHTLEVEL,type:"满潮",cls:"high"},
    {t:item.FIRSTLOWTIME,l:item.FIRSTLOWLEVEL,type:"低潮",cls:"low"},
    {t:item.SECONDLOWTIME,l:item.SECONDLOWLEVEL,type:"低潮",cls:"low"}
  ].filter(function(i){return i.t&&i.t!=="-";}).sort(function(a,b){return (timeToMin(a.t)!=null?timeToMin(a.t):9999)-(timeToMin(b.t)!=null?timeToMin(b.t):9999);});
  lastTideList=list;
  calcTideStatus(list);
}
function interpolateCurrentLevel(points, nowMin){
  var curve=(points||[]).filter(function(p){return p.minute!==null&&!isNaN(p.value);}).sort(function(a,b){return a.minute-b.minute;});
  if(!curve.length)return null;
  if(nowMin<=curve[0].minute)return Math.round(curve[0].value);
  if(nowMin>=curve[curve.length-1].minute)return Math.round(curve[curve.length-1].value);
  for(var i=1;i<curve.length;i++){
    var prev=curve[i-1], next=curve[i];
    if(nowMin>=prev.minute&&nowMin<=next.minute){
      var ratio=(nowMin-prev.minute)/Math.max(1,next.minute-prev.minute);
      return Math.round(prev.value+(next.value-prev.value)*ratio);
    }
  }
  return null;
}
function calcTideStatus(list){
  if(document.querySelector("#tideCard .module-unavailable"))return;
  var now=new Date(), nowMin=selectedDayOffset===0 ? now.getHours()*60+now.getMinutes() : 0;
  if(selectedDayOffset===0){
    var statusTime=String(now.getMonth()+1).padStart(2,"0")+"-"+String(now.getDate()).padStart(2,"0")+" "+String(now.getHours()).padStart(2,"0")+":"+String(now.getMinutes()).padStart(2,"0");
    setText("tideUpdate","状态 "+statusTime);
  }
  var pts=list.map(function(x){return {min:timeToMin(x.t),height:Number(x.l),type:x.type,time:formatHHMM(x.t)};}).filter(function(p){return p.min!==null&&!isNaN(p.height);}).sort(function(a,b){return a.min-b.min;});
  if(pts.length<2){setText("tideBadge","数据不足");return;}
  var curve=(lastChartPoints||[]).filter(function(p){return p.minute!==null&&!isNaN(p.value);}).sort(function(a,b){return a.minute-b.minute;});

  // 如果是今日模式且有明日数据，将明日第一个高低潮加入计算（用于末段进度）
  var hasTomorrowNext=false;
  if(selectedDayOffset===0 && tomorrowTideList.length>0){
    var tomPts=tomorrowTideList.map(function(x){return {min:timeToMin(x.t)+1440,height:Number(x.l),type:x.type,time:formatHHMM(x.t),isTomorrow:true};}).filter(function(p){return p.min!==null&&!isNaN(p.height);}).sort(function(a,b){return a.min-b.min;});
    if(tomPts.length>0){
      pts=pts.concat(tomPts);
      hasTomorrowNext=true;
    }
  }

  var prev=pts[0],next=pts[pts.length-1];
  var futureExtrema=pts.filter(function(p){return p.min>nowMin;});
  if(futureExtrema.length){
    next=futureExtrema[0];
    var prevArr=pts.filter(function(p){return p.min<=nowMin;}); prev=prevArr.length?prevArr[prevArr.length-1]:pts[0];
  }else{
    prev=pts[pts.length-1];
    next=null;
  }
  var rising=false;
  if(curve.length>=2){
    var cPrev=curve[0], cNext=curve[1];
    if(nowMin>=curve[curve.length-1].minute){
      cPrev=curve[curve.length-2];
      cNext=curve[curve.length-1];
    }else{
      for(var i=1;i<curve.length;i++){
        if(nowMin<=curve[i].minute){cPrev=curve[i-1];cNext=curve[i];break;}
      }
    }
    rising=cNext.value-cPrev.value>0;
  }else if(next){
    rising=next.height-prev.height>0;
  }
  setText("tideBadge",selectedDayOffset===0 ? (rising?"涨潮中":"退潮中") : "明日预报");
  var badgeEl=$("tideBadge");
  if(badgeEl){
    badgeEl.classList.remove("rising","falling");
    if(selectedDayOffset===0){
      badgeEl.classList.add(rising?"rising":"falling");
    }
  }
  var statusCell=badgeEl?badgeEl.closest(".tide-status-cell"):null;
  if(statusCell){
    statusCell.classList.remove("is-rising","is-falling");
    if(selectedDayOffset===0){
      statusCell.classList.add(rising?"is-rising":"is-falling");
    }
  }
  if(selectedDayOffset===0&&lastTideRising!==null&&rising&&!lastTideRising&&soundEnabled){playRisingSound();}
  lastTideRising=rising;
  if(next){
    if(prev&&prev.min<nowMin&&prev.min<next.min){
      var phaseTotal=Math.max(1,next.min-prev.min);
      var phaseProgress=Math.max(0,Math.min(100,Math.round((nowMin-prev.min)/phaseTotal*100)));
      setText("tideProgress",phaseProgress+"%");
    }else{
      setText("tideProgress","0%");
      phaseProgress=0;
    }
  }else{
    setText("tideProgress","--");
    phaseProgress=0;
  }
  var waterBg=$("tideWaterBg");
  if(waterBg){
    var waterHeight=0;
    if(rising){
      waterHeight=phaseProgress||0;
    }else{
      waterHeight=100-(phaseProgress||0);
    }
    waterBg.style.height=waterHeight+"%";
  }
  var progressCell=waterBg?waterBg.closest(".tide-progress-cell"):null;
  if(progressCell){
    progressCell.classList.remove("is-rising","is-falling");
    if(selectedDayOffset===0){
      progressCell.classList.add(rising?"is-rising":"is-falling");
    }
  }
  var currentLevel=interpolateCurrentLevel(curve,nowMin);
  setTextWithUnit("currentLevel",currentLevel!==null?currentLevel:"--","cm");
  var highs=pts.filter(function(p){return p.type==="满潮";}), lows=pts.filter(function(p){return p.type==="低潮";});
  var nextHigh=null,nextLow=null;
  for(var hi=0;hi<highs.length;hi++){if(highs[hi].min>nowMin){nextHigh=highs[hi];break;}}
  for(var li=0;li<lows.length;li++){if(lows[li].min>nowMin){nextLow=lows[li];break;}}

  // 如果今日没有更多高低潮，且是今日模式，则从明日数据中取第一个
  if(selectedDayOffset===0 && tomorrowTideList.length>0){
    var tomHighs=tomorrowTideList.filter(function(p){return p.type==="满潮";});
    var tomLows=tomorrowTideList.filter(function(p){return p.type==="低潮";});
    if(!nextHigh && tomHighs.length>0){
      nextHigh={t:tomHighs[0].t,l:tomHighs[0].l,type:"满潮",min:timeToMin(tomHighs[0].t)+1440,isTomorrow:true};
    }
    if(!nextLow && tomLows.length>0){
      nextLow={t:tomLows[0].t,l:tomLows[0].l,type:"低潮",min:timeToMin(tomLows[0].t)+1440,isTomorrow:true};
    }
  }

  var nextHighText=nextHigh?((nextHigh.isTomorrow?"明日 ":"")+(nextHigh.time||formatHHMM(nextHigh.t))):"--";
  var nextLowText=nextLow?((nextLow.isTomorrow?"明日 ":"")+(nextLow.time||formatHHMM(nextLow.t))):"--";
  var nextHighLevelText=nextHigh?(nextHigh.height||nextHigh.l||"--"):"--";
  var nextLowLevelText=nextLow?(nextLow.height||nextLow.l||"--"):"--";
  setText("nextHigh",nextHighText);
  setText("nextLow",nextLowText);
  setTextWithUnit("nextHighLevel",nextHighLevelText,"cm");
  setTextWithUnit("nextLowLevel",nextLowLevelText,"cm");
  var highDeltaText="--";
  var lowDeltaText="--";
  if(nextHigh){
    if(nextHigh.isTomorrow){
      var minLeft=nextHigh.min-nowMin;
      highDeltaText=formatDuration(minLeft);
    }else{
      highDeltaText=formatDuration(nextHigh.min-nowMin);
    }
  }else{
    highDeltaText=selectedDayLabel()+"无";
  }
  if(nextLow){
    if(nextLow.isTomorrow){
      var minLeftL=nextLow.min-nowMin;
      lowDeltaText=formatDuration(minLeftL);
    }else{
      lowDeltaText=formatDuration(nextLow.min-nowMin);
    }
  }else{
    lowDeltaText=selectedDayLabel()+"无";
  }
  setText("highDelta",highDeltaText);
  setText("lowDelta",lowDeltaText);
  renderTideSummary(pts);
}
function initAudio(){
  if(!audioCtx) audioCtx=new (window.AudioContext||window.webkitAudioContext)();
}
function playRisingSound(){
  try{
    initAudio();
    if(audioCtx.state==="suspended") audioCtx.resume();
    var t=audioCtx.currentTime;
    var osc1=audioCtx.createOscillator(); var g1=audioCtx.createGain();
    osc1.type="sine"; osc1.frequency.setValueAtTime(523,t); osc1.frequency.exponentialRampToValueAtTime(784,t+0.15);
    g1.gain.setValueAtTime(0.08,t); g1.gain.exponentialRampToValueAtTime(0.001,t+0.5);
    osc1.connect(g1); g1.connect(audioCtx.destination); osc1.start(t); osc1.stop(t+0.5);
    var osc2=audioCtx.createOscillator(); var g2=audioCtx.createGain();
    osc2.type="sine"; osc2.frequency.setValueAtTime(659,t+0.18); osc2.frequency.exponentialRampToValueAtTime(1047,t+0.35);
    g2.gain.setValueAtTime(0.06,t+0.18); g2.gain.exponentialRampToValueAtTime(0.001,t+0.65);
    osc2.connect(g2); g2.connect(audioCtx.destination); osc2.start(t+0.18); osc2.stop(t+0.65);
  }catch(e){}
}
function toggleSound(){
  soundEnabled=!soundEnabled; initAudio();
  var btn=$("soundBtn"); if(btn){
    btn.innerHTML=soundEnabled?'🔊 <span>声音开</span>':'🔇 <span>声音关</span>';
    btn.setAttribute("aria-pressed",soundEnabled?"true":"false");
  }
}
function formatDuration(minutes){
  if(minutes===null||minutes===undefined||minutes<0)return "--";
  var h=Math.floor(minutes/60), m=minutes%60;
  if(h<=0)return m+"分钟";
  return h+"小时"+(m>0?m+"分":"");
}
function renderTideSummary(pts){
  // 新布局不再需要tideRange/todayHighLevel/todayLowLevel，保留函数以防调用报错
}
function parseChartPoints(rawArr){
  var arr=Array.isArray(rawArr)?rawArr:[]; var all=arr.map(function(it){
    var parsed=parseTidePointTime(it&&it.TIDETIME), val=Number(it&&it.TIDEHEIGHT||0); if(!parsed)return null;
    return {dateKey:normalizeDateKey(it&&it.TIDEDATE||""),label:parsed.label,minute:parsed.minute,value:isNaN(val)?0:val,pointType:it&&it.POINT_TYPE||"hour",extremaType:it&&it.EXTREMA_TYPE||""};
  }).filter(Boolean);
  var day=todayKey(); var points=all.filter(function(p){return p.dateKey===day;}); if(points.length===0&&all.length>0)points=all.filter(function(p){return p.dateKey===all[0].dateKey;});
  points.sort(function(a,b){return a.minute-b.minute;});
  // 如果最后一个点不足23:59，补充一个23:59的点，使X轴延伸到全天结束
  if(points.length>0&&points[points.length-1].minute<1439){
    var last=points[points.length-1];
    points.push({dateKey:last.dateKey,label:"23:59",minute:1439,value:last.value,pointType:last.pointType,extremaType:""});
  }
  return points;
}
function adaptiveChartFont(base,min,max){var scale=Math.min(window.innerWidth/1280,window.innerHeight/760);return Math.max(min,Math.min(max,Math.round(base*scale*1.4)));}
function initChart(){
  if(typeof echarts==="undefined")return false; if(!tideChart){tideChart=echarts.init($("tideChart")); window.addEventListener("resize",function(){tideChart.resize();clearTimeout(resizeTimer);resizeTimer=setTimeout(function(){if(lastChartRaw)renderChart(lastChartRaw,"",lastChartSite);},160);});}
  return true;
}
function renderChart(rawArr,msg,site){
  lastChartRaw=Array.isArray(rawArr)?rawArr:null; lastChartSite=site||null; var points=parseChartPoints(rawArr);
  lastChartPoints=points;
  if(lastTideList.length) calcTideStatus(lastTideList);
  setText("chartSource",site&&site.code?site.name+"("+site.code+")":"全球潮汐平台");
  if(!points.length){$("tideChart").innerText=msg||"暂无实时曲线数据";return;}
  if(!initChart()){ $("tideChart").innerText="ECharts 加载中"; return; }
  var axisFont=adaptiveChartFont(14,11,18), markFont=adaptiveChartFont(13,10,16), nameFont=adaptiveChartFont(15,12,19);
  var maxVal=Math.max.apply(null,points.map(function(p){return p.value;}));
  var isMobileView=document.documentElement.classList.contains("mobile");
  var gridLeft=isMobileView?30:52, gridRight=isMobileView?30:52, gridTop=isMobileView?36:48, gridBottom=isMobileView?32:40;
  var markData=points.filter(function(p){return p.pointType==="extrema";}).map(function(p){var isHigh=p.extremaType==="满潮";return {name:p.extremaType,coord:[p.label,p.value],value:p.value,labelText:p.extremaType+" "+p.label+"\n"+p.value+"cm",itemStyle:{color:isHigh?"#ff5252":"#00e676",shadowBlur:12,shadowColor:isHigh?"rgba(255,82,82,.6)":"rgba(0,230,118,.6)"},label:{formatter:function(params){return params.data.labelText;},color:isHigh?"#ff5252":"#00e676",fontSize:markFont,fontWeight:"bold",lineHeight:markFont+1,position:"right",distance:4,offset:[0,-14],textShadowColor:"rgba(0,0,0,.85)",textShadowBlur:6,textShadowOffsetX:0,textShadowOffsetY:1}};});
  tideChart.setOption({
    backgroundColor:"transparent",
    tooltip:{trigger:"axis",formatter:function(p){return "时间："+p[0].axisValue+"<br>潮高："+p[0].value+" cm";},backgroundColor:"rgba(15,21,40,.94)",borderColor:"rgba(0,229,255,.35)",textStyle:{color:"#e8eaf6",fontSize:axisFont}},
    grid:{left:gridLeft,right:gridRight,top:gridTop,bottom:gridBottom,containLabel:true},
    xAxis:{type:"category",data:points.map(function(p){return p.label;}),axisLabel:{rotate:0,interval:2,fontSize:axisFont,margin:6,color:"rgba(232,234,246,.75)",textShadowColor:"rgba(0,0,0,.7)",textShadowBlur:4,textShadowOffsetX:0,textShadowOffsetY:1},axisLine:{lineStyle:{color:"rgba(0,229,255,.28)"}},axisTick:{lineStyle:{color:"rgba(0,229,255,.22)"}}},
    yAxis:{name:"潮高(cm)",type:"value",max:Math.ceil((maxVal+35)/50)*50,nameTextStyle:{fontSize:nameFont,color:"rgba(232,234,246,.75)",textShadowColor:"rgba(0,0,0,.7)",textShadowBlur:4,textShadowOffsetX:0,textShadowOffsetY:1},axisLabel:{fontSize:axisFont,color:"rgba(232,234,246,.75)",textShadowColor:"rgba(0,0,0,.7)",textShadowBlur:4,textShadowOffsetX:0,textShadowOffsetY:1},axisLine:{lineStyle:{color:"rgba(0,229,255,.28)"}},splitLine:{lineStyle:{color:"rgba(255,255,255,.07)"}}},
    series:[{name:"潮高",type:"line",data:points.map(function(p){return p.value;}),smooth:true,symbolSize:6,itemStyle:{color:"#00e5ff",shadowBlur:10,shadowColor:"rgba(0,229,255,.5)"},lineStyle:{color:"#00e5ff",width:3,shadowBlur:14,shadowColor:"rgba(0,229,255,.55)"},areaStyle:{color:{type:"linear",colorStops:[{offset:0,color:"rgba(0,229,255,.28)"},{offset:1,color:"rgba(0,229,255,.03)"}]}},markPoint:{symbol:"circle",symbolSize:24,data:markData},markLine:{symbol:"none",silent:true,data:[],lineStyle:{color:"#ffab00",width:2,type:"solid",shadowBlur:10,shadowColor:"rgba(255,171,0,.6)"},label:{show:true,formatter:"现在",color:"#ffab00",fontSize:markFont,fontWeight:"bold",position:"end",distance:[4,0],backgroundColor:"rgba(6,10,20,.85)",padding:[3,8,3,8],borderRadius:3,textShadowColor:"rgba(0,0,0,.85)",textShadowBlur:6,textShadowOffsetX:0,textShadowOffsetY:1}}}]
  },true);
  // 启动当前时间标线更新（明日模式下不显示）
  if(selectedDayOffset===0){
    startNowMarkLine();
  }else{
    if(nowMarkLineTimer){clearInterval(nowMarkLineTimer);nowMarkLineTimer=null;}
    if(tideChart)tideChart.setOption({series:[{markLine:{data:[]}}]});
  }
}
var nowMarkLineTimer=null;
function startNowMarkLine(){
  if(!tideChart||!lastChartPoints||!lastChartPoints.length)return;
  if(selectedDayOffset!==0)return;
  updateNowMarkLine();
  if(nowMarkLineTimer)clearInterval(nowMarkLineTimer);
  nowMarkLineTimer=setInterval(updateNowMarkLine,60000);
}
function updateNowMarkLine(){
  if(!tideChart||!lastChartPoints||!lastChartPoints.length)return;
  if(selectedDayOffset!==0){
    tideChart.setOption({series:[{markLine:{data:[]}}]});
    return;
  }
  var now=new Date();
  var nowHm=("0"+now.getHours()).slice(-2)+":"+("0"+now.getMinutes()).slice(-2);
  var points=lastChartPoints;
  // 找到当前时间在x轴上的位置（在两个数据点之间插值）
  var nowIdx=-1;
  var nowRatio=0;
  for(var i=0;i<points.length-1;i++){
    var t1=parseChartTime(points[i].label);
    var t2=parseChartTime(points[i+1].label);
    var tNow=parseChartTime(nowHm);
    // 处理跨天的情况
    if(t2<t1)t2+=24*60;
    if(tNow<t1)tNow+=24*60;
    if(tNow>=t1&&tNow<=t2){
      nowIdx=i;
      nowRatio=(tNow-t1)/(t2-t1);
      break;
    }
  }
  if(nowIdx<0){
    // 当前时间不在数据范围内，隐藏标线
    tideChart.setOption({series:[{markLine:{data:[]}}]});
    return;
  }
  // 计算当前潮高（插值）
  var v1=points[nowIdx].value;
  var v2=points[nowIdx+1].value;
  var nowVal=v1+(v2-v1)*nowRatio;
  // 用xAxis的category索引定位标线
  tideChart.setOption({series:[{markLine:{data:[{xAxis:nowIdx+nowRatio,label:{formatter:"现在 "+nowHm}}]}}]});
}
function parseChartTime(hm){
  var parts=hm.split(":");
  return parseInt(parts[0])*60+parseInt(parts[1]);
}
function clearModuleUnavailable(cardId){var card=$(cardId);if(card){var h=card.querySelector(".module-unavailable");if(h)h.parentNode.removeChild(h);}}
function setAllText(ids, text){for(var i=0;i<ids.length;i++){setText(ids[i],text);}}
function showWeatherUnavailable(){
  clearModuleUnavailable("weatherCard");
  setAllText(["tempRange","humidity","windDirection"],"未知");
  setText("weatherTime","暂无明日数据"); setText("weatherText","未知");
  var icon=$("weatherIcon"); if(icon) icon.className="weather-icon";
}
function showWaveUnavailable(){
  setText("offshoreWaveHeight","--");
  setText("beachWaterTemp","--");
}
function showTideUnavailable(){
  clearModuleUnavailable("tideCard");
  setAllText(["tideBadge","nextHigh","nextLow","tideProgress","highDelta","lowDelta"],"未知");
  setTextWithUnit("currentLevel","--","cm");
  setTextWithUnit("nextHighLevel","--","cm");
  setTextWithUnit("nextLowLevel","--","cm");
  setText("tideUpdate","暂无明日数据"); setText("globalUpdate","数据更新 --");
}
function showChartUnavailable(){
  clearModuleUnavailable("chartCard");
  setText("chartTime","暂无明日数据");
  var tc=$("tideChart"); if(tc) tc.innerHTML='<div class="module-unavailable">暂无明日数据</div>';
}
function loadWeather(){fetchJSON(apiUrl("/api/weather"),20000,function(e,r){if(r&&r.tomorrow_unavailable){showWeatherUnavailable();return;}if(r&&r.data)renderWeather(r.data,r.updateTime);});}
function loadWave(){fetchJSON(apiUrl("/api/wave"),20000,function(e,r){if(r&&r.tomorrow_unavailable){showWaveUnavailable();return;}if(r&&r.data)renderWave(r.data,r.updateTime);});}
function loadOffshoreWave(){fetchJSON(apiUrl("/api/offshore_wave"),20000,function(e,r){if(e||!r||!r.data)return;var wh=r.data.wave_height||"--";setText("offshoreWaveHeight",wh);});}
function loadAlarm(){fetchJSON(apiUrl("/api/alarm"),20000,function(e,r){});}
function loadSdAlarm(){fetchJSON("/api/sd_alarm",45000,function(e,r){
  var listCard=$("alarmListCard");var container=$("alarmListContainer");var timeEl=$("alarmListTime");
  if(!r||e||!r.data||!Array.isArray(r.data)||r.data.length===0){
    if(container)container.innerHTML='<div class="alarm-list-empty">暂无预警信息</div>';
    if(timeEl)timeEl.textContent=r&&r.updateTime?r.updateTime:"--";
    return;
  }
  // 列表形式展示
  var html="";
  for(var i=0;i<r.data.length;i++){
    var item=r.data[i];
    var levelCls="blue";
    var levelText="蓝色";
    var lv=(item.level||"")+(item.title||"");
    if(lv.indexOf("红色")>=0){levelCls="red";levelText="红色";}
    else if(lv.indexOf("橙色")>=0){levelCls="orange";levelText="橙色";}
    else if(lv.indexOf("黄色")>=0){levelCls="yellow";levelText="黄色";}
    var type=item.type||"气象预警";
    var title=item.title||"--";
    var pubTime=item.publish_time||"--";
    var idx=i;
    html+='<div class="alarm-list-item item-'+levelCls+'" onclick="openAlarmModal('+idx+')">';
    html+='<span class="alarm-level-tag '+levelCls+'">'+levelText+'</span>';
    html+='<div class="alarm-item-body">';
    html+='<div class="alarm-item-title">'+title+'</div>';
    html+='<div class="alarm-item-meta">';
    html+='<span class="alarm-item-type">'+type+'</span>';
    html+='<span class="alarm-item-time">'+pubTime+'</span>';
    html+='</div>';
    html+='</div>';
    html+='</div>';
  }
  if(container)container.innerHTML=html;
  if(timeEl)timeEl.textContent=r.updateTime||"--";
  // 缓存数据供弹窗使用
  window._alarmData=r.data;
});}

// 统一格式化预警时间为 YYYY-MM-DD HH:MM
function formatAlarmTime(t){
  if(!t||t==="--")return "";
  var s=String(t).trim();
  if(!s)return "";
  s=s.replace(/年|月|\/|\./g,"-").replace(/日/g," ").replace(/时/g,":").replace(/分/g,"").replace(/\s+/g," ").trim();
  var parts=s.split(" ");
  var d=parts[0]?parts[0].split("-"):[];
  if(d.length<3)return t;
  var y=parseInt(d[0]),mo=parseInt(d[1]),da=parseInt(d[2]);
  if(isNaN(y)||isNaN(mo)||isNaN(da))return t;
  var yStr=String(y),moStr=String(mo).padStart(2,"0"),daStr=String(da).padStart(2,"0");
  var timePart="";
  if(parts.length>1&&parts[1]){
    var tp=parts[1].split(":");
    var h=parseInt(tp[0])||0,m=parseInt(tp[1])||0;
    timePart=" "+String(h).padStart(2,"0")+":"+String(m).padStart(2,"0");
  }
  return yStr+"-"+moStr+"-"+daStr+timePart;
}

function loadCmaAlarm(){
  var marquee=$("cmaAlarmMarquee");
  var bar=$("cmaAlarmBar");
  var timeEl=$("cmaAlarmTime");
  if(!marquee)return;
  var cmaData=[],sdData=[],qdData=[],cmaDone=false,sdDone=false,qdDone=false,cmaTime="",sdTime="",qdTime="";
  function tryRender(){
    if(!cmaDone||!sdDone||!qdDone)return;
    var allData=[];
    for(var i=0;i<cmaData.length;i++){allData.push(cmaData[i]);}
    for(var j=0;j<sdData.length;j++){allData.push(sdData[j]);}
    for(var m=0;m<qdData.length;m++){allData.push(qdData[m]);}
    // 按发布时间倒序排序（最新的在前）
    function parseAlarmTime(t){
      if(!t||t==="--")return -1;
      var s=String(t).trim();
      if(!s)return -1;
      // 统一格式：替换斜杠、年月日等分隔符
      s=s.replace(/年|月|\/|\./g,"-").replace(/日/g," ").replace(/时/g,":").replace(/\s+/g," ").trim();
      // 提取日期和时间部分
      var dateStr="",timeStr="";
      var sp=s.split(" ");
      dateStr=sp[0]||"";
      if(sp.length>1)timeStr=sp[1]||"";
      var d=dateStr.split("-");
      if(d.length<3)return -1;
      var y=parseInt(d[0]),mo=parseInt(d[1]),da=parseInt(d[2]);
      if(isNaN(y)||isNaN(mo)||isNaN(da))return -1;
      var h=0,mi=0,se=0;
      if(timeStr){
        var t2=timeStr.split(":");
        h=parseInt(t2[0])||0;
        mi=parseInt(t2[1])||0;
        se=parseInt(t2[2])||0;
      }
      return new Date(y,mo-1,da,h,mi,se).getTime();
    }
    allData.sort(function(a,b){
      var ta=parseAlarmTime(a.publish_time);
      var tb=parseAlarmTime(b.publish_time);
      if(ta<0&&tb<0)return 0;
      if(ta<0)return 1;
      if(tb<0)return -1;
      return tb-ta;
    });
    // 只保留最近3天的预警
    var threeDaysAgo=Date.now()-3*24*60*60*1000;
    allData=allData.filter(function(item){
      var t=parseAlarmTime(item.publish_time);
      return t<0||t>=threeDaysAgo;
    });
    if(allData.length===0){
      marquee.innerHTML='<span class="cma-alarm-item item-blue">暂无预警信息</span>';
      if(bar)bar.style.display="none";
      if(timeEl)timeEl.textContent="更新 --";
      window._cmaAlarmData=[];
      return;
    }
    if(bar)bar.style.display="flex";
    var levelOrder={red:4,orange:3,yellow:2,blue:1,green:0};
    var maxLevel="blue";
    var html="";
    for(var k=0;k<allData.length;k++){
      var item=allData[k];
      var levelCls="blue";
      var levelText="蓝色";
      var lv=(item.level||"")+(item.title||"");
      if(lv.indexOf("红色")>=0){levelCls="red";levelText="红色";}
      else if(lv.indexOf("橙色")>=0){levelCls="orange";levelText="橙色";}
      else if(lv.indexOf("黄色")>=0){levelCls="yellow";levelText="黄色";}
      else if(lv.indexOf("解除")>=0){levelCls="green";levelText="解除";}
      else if(lv.indexOf("消息")>=0){levelCls="blue";levelText="消息";}
      if(levelOrder[levelCls]>levelOrder[maxLevel])maxLevel=levelCls;
      var title=item.title||"--";
      var pubTime=formatAlarmTime(item.publish_time);
      var source=item.source?"<small style=\"opacity:.45;margin-left:6px;\">["+item.source+"]</small>":"";
      html+='<span class="cma-alarm-item item-'+levelCls+'" onclick="openCmaAlarmModal('+k+')">';
      html+='<span class="lvl-'+levelCls+'">'+levelText+'</span>';
      html+=title;
      if(pubTime)html+=' <small style="opacity:.55;margin-left:4px;">'+pubTime+'</small>';
      html+=source;
      html+='</span>';
    }
    if(bar){
      bar.classList.remove("bar-blue","bar-yellow","bar-orange","bar-red");
      bar.classList.add("bar-"+maxLevel);
    }
    marquee.innerHTML=html;
    window._cmaAlarmData=allData;
    var latestTime=cmaTime||sdTime||qdTime;
    if(timeEl)timeEl.textContent="更新 "+(latestTime||"--");
  }
  fetchJSON("/api/cma_alarm",30000,function(e,r){
    cmaDone=true;
    if(!e&&r&&r.data&&Array.isArray(r.data)){
      cmaData=r.data;
      cmaTime=r.updateTime||"";
    }
    tryRender();
  });
  fetchJSON("/api/sd_alarm",45000,function(e,r){
    sdDone=true;
    if(!e&&r&&r.data&&Array.isArray(r.data)){
      sdData=r.data.map(function(it){if(!it.source)it.source="山东省气象台";return it;});
      sdTime=r.updateTime||"";
    }
    tryRender();
  });
  fetchJSON("/api/alarm",30000,function(e,r){
    qdDone=true;
    if(!e&&r&&r.data&&Array.isArray(r.data)){
      qdData=r.data.map(function(it){it.source="青岛海洋预报台";return it;});
      qdTime=r.updateTime||"";
    }
    tryRender();
  });
}

function openCmaAlarmModal(index){
  var data=window._cmaAlarmData||[];
  var item=data[index];
  if(!item){return;}
  var modal=$("alarmModal");
  if(!modal){return;}
  modal.style.display="flex";
  $("alarmModalTitle").textContent=item.title||"预警详情";
  $("alarmModalTime").textContent=formatAlarmTime(item.publish_time)||"--";
  var linkEl=$("alarmModalLink");
  if(item.url){
    _currentAlarmUrl=item.url;
    linkEl.href=item.url;
    linkEl.target="_blank";
    linkEl.rel="noopener noreferrer";
    linkEl.classList.remove("disabled");
  }else{
    _currentAlarmUrl="";
    linkEl.href="javascript:void(0)";
    linkEl.classList.add("disabled");
  }
  var bodyHtml="";
  bodyHtml+="<p><strong>预警类型：</strong>"+(item.type||"气象预警")+"</p>";
  bodyHtml+="<p><strong>预警级别：</strong>"+(item.level||"--")+"</p>";
  bodyHtml+="<p><strong>发布时间：</strong>"+(item.publish_time||"--")+"</p>";
  if(item.source)bodyHtml+="<p><strong>信息来源：</strong>"+item.source+"</p>";
  bodyHtml+="<div id=\"alarmDetailContent\" style=\"margin-top:12px;padding-top:12px;border-top:1px solid rgba(255,255,255,.1);color:rgba(232,234,246,.75);line-height:1.8;\"><em style=\"color:rgba(232,234,246,.4);\">正在加载详情...</em></div>";
  $("alarmModalBody").innerHTML=bodyHtml;
  
  // 青岛海洋预报台docx文件直接提示查看原文
  var isDocx=item.url&&(item.url.indexOf("Alermfile.aspx")>=0||item.url.toLowerCase().indexOf(".docx")>=0||item.url.toLowerCase().indexOf(".doc")>=0);
  if(isDocx){
    $("alarmDetailContent").innerHTML="<p style=\"color:rgba(232,234,246,.7);\">预警详情为文档格式，请点击下方「查看原文」按钮打开。</p>";
    return;
  }
  // 加载预警详情内容
  if(item.url){
    var detailUrl="/api/cma_alarm_detail?url="+encodeURIComponent(item.url);
    fetch(detailUrl,{cache:"no-cache"})
      .then(function(r){return r.json();})
      .then(function(res){
        if(res&&res.success&&res.data&&res.data.content){
          var content=res.data.content;
          // 处理换行：先统一换行符，再转换为HTML段落
          content=content.replace(/\\r\\n/g,"\\n").replace(/\\r/g,"\\n");
          var paras=content.split(/\\n\\s*\\n/).filter(function(p){return p.trim().length>0;});
          if(paras.length>1){
            content=paras.map(function(p){return "<p>"+p.replace(/\\n/g,"<br>")+"</p>";}).join("");
          }else{
            content="<p>"+content.replace(/\\n/g,"<br>")+"</p>";
          }
          $("alarmDetailContent").innerHTML=content;
        }else{
          $("alarmDetailContent").innerHTML="<p style=\"color:rgba(232,234,246,.4);\">暂无法获取详情内容，请点击下方查看原文链接</p>";
        }
      })
      .catch(function(){
        $("alarmDetailContent").innerHTML="<p style=\"color:rgba(232,234,246,.4);\">详情加载失败，请点击下方查看原文链接</p>";
      });
  }else{
    $("alarmDetailContent").innerHTML="<p style=\"color:rgba(232,234,246,.4);\">暂无详情内容</p>";
  }
}

var _alarmData=[];
function openAlarmModal(index){
  var data=window._alarmData||[];
  var item=data[index];
  if(!item){return;}
  var modal=$("alarmModal");
  if(!modal){return;}
  modal.style.display="flex";
  $("alarmModalTitle").textContent=item.title||"预警详情";
  $("alarmModalTime").textContent=formatAlarmTime(item.publish_time)||"--";
  _currentAlarmUrl=item.url||"";
  var sdLinkEl=$("alarmModalLink");
  if(item.url){
    sdLinkEl.href=item.url;
    sdLinkEl.target="_blank";
    sdLinkEl.rel="noopener noreferrer";
    sdLinkEl.classList.remove("disabled");
  }else{
    sdLinkEl.href="javascript:void(0)";
    sdLinkEl.classList.add("disabled");
  }
  $("alarmModalBody").innerHTML='<div class="alarm-modal-loading">加载中...</div>';
  // 获取详情内容
  var url="/api/sd_alarm_detail?url="+encodeURIComponent(item.url||"");
  fetchJSON(url,15000,function(e,r){
    if(!r||e||!r.data){
      $("alarmModalBody").innerHTML='<div style="text-align:center;color:rgba(232,234,246,.5);padding:20px 0;">加载失败，请点击"查看原文"查看</div>';
      return;
    }
    var content=r.data.content||"暂无详情内容";
    // 将换行转换为段落
    var paragraphs=content.split(/\n\s*\n/).filter(function(p){return p.trim().length>0;});
    var html="";
    for(var i=0;i<paragraphs.length;i++){
      html+="<p>"+paragraphs[i].replace(/\n/g,"<br>")+"</p>";
    }
    if(r.data.title&&r.data.title!==item.title){
      $("alarmModalTitle").textContent=r.data.title;
    }
    if(r.data.pub_time&&r.data.pub_time!=="--"){
      $("alarmModalTime").textContent=formatAlarmTime(r.data.pub_time);
    }
    $("alarmModalBody").innerHTML=html;
  });
}

function closeAlarmModal(){
  var modal=$("alarmModal");
  if(modal){modal.style.display="none";}
}

// 打开预警原文链接
var _currentAlarmUrl="";
function openAlarmOriginal(){
  if(!_currentAlarmUrl||_currentAlarmUrl==="#"||_currentAlarmUrl==="javascript:void(0)")return;
  window.open(_currentAlarmUrl,"_blank");
}

// ESC键关闭弹窗
document.addEventListener("keydown",function(e){
  if(e.key==="Escape"){closeAlarmModal();}
});
function loadTide(){fetchJSON(apiUrl("/api/tide"),20000,function(e,r){if(!r||e)return;if(r&&r.tomorrow_unavailable){showTideUnavailable();return;}if(r&&r.data)renderTide(r,r.updateTime);});
  // 预加载明日潮汐数据，用于计算次日高低潮
  if(selectedDayOffset===0){
    var tomorrowUrl="/api/tide?date="+(function(){var d=new Date();d.setDate(d.getDate()+1);return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");})();
    fetchJSON(tomorrowUrl,20000,function(e,r){
      tomorrowTideReady=true;
      if(!r||e||!r.data||!r.data.rows||!r.data.rows.length){tomorrowTideList=[];return;}
      var item=r.data.rows[0];
      tomorrowTideList=[
        {t:item.FIRSTHIGHTIME,l:item.FIRSTHIGHLEVEL,type:"满潮",cls:"high"},
        {t:item.SECONDHIGHTIME,l:item.SECONDHEIGHTLEVEL,type:"满潮",cls:"high"},
        {t:item.FIRSTLOWTIME,l:item.FIRSTLOWLEVEL,type:"低潮",cls:"low"},
        {t:item.SECONDLOWTIME,l:item.SECONDLOWLEVEL,type:"低潮",cls:"low"}
      ].filter(function(i){return i.t&&i.t!=="-";}).sort(function(a,b){return (timeToMin(a.t)!=null?timeToMin(a.t):9999)-(timeToMin(b.t)!=null?timeToMin(b.t):9999);});
    });
  }
}
function loadChart(){fetchJSON(apiUrl("/api/tideChart"),20000,function(e,r){if(!r||e){renderChart([],"潮汐曲线加载失败",null);return;}if(r&&r.tomorrow_unavailable){showChartUnavailable();return;}setText("chartTime","更新 "+(r.updateTime||"--"));renderChart(r.chart,r.msg,r.site);});}
function refreshAllData(){
  var btn=$("refreshBtn");
  if(btn){btn.disabled=true;btn.classList.add("is-loading");btn.textContent="↻ 刷新中";}
  loadTide();loadChart();loadWeather();loadWave();loadOffshoreWave();loadCmaAlarm();
  setTimeout(function(){
    reloadTyphoonFrame();
    if(btn)btn.textContent="✓ 已更新";
    setTimeout(function(){if(btn){btn.textContent="↻ 刷新";btn.disabled=false;btn.classList.remove("is-loading");}},1200);
  },2000);
}
function boot(){
  // 检测 User-Agent 判断是否移动端
  var ua=navigator.userAgent||"";
  var isMobile=/(Android|iPhone|iPad|iPod|Mobile|Opera Mini|IEMobile|WPDesktop|BlackBerry|BB10|SymbianOS|Series60|Windows Phone)/i.test(ua);
  // Android 平板（无 Mobile 标记）通过屏幕尺寸辅助判断
  if(!isMobile&&/Android/i.test(ua)){isMobile=Math.max(screen.width,screen.height)<1080;}
  if(isMobile){document.documentElement.className+=" mobile";}
  updateClock(); setInterval(updateClock,1000);
  updateDayButtons();
  loadTide(); loadChart(); loadWeather(); loadWave(); loadOffshoreWave(); loadCmaAlarm(); loadSdAlarm();
  setInterval(loadTide,60*60*1000); setInterval(loadChart,6*60*60*1000); setInterval(loadWeather,10*60*1000); setInterval(loadWave,60*60*1000); setInterval(loadOffshoreWave,60*60*1000); setInterval(loadCmaAlarm,5*60*1000); setInterval(loadSdAlarm,5*60*1000);
  setInterval(function(){if(lastTideList.length)calcTideStatus(lastTideList);},60*1000);
  setTimeout(function(){if(lastChartRaw)renderChart(lastChartRaw,"",lastChartSite);},1000);
}
boot();
