// Polyfills for Android 4.4.2 (Chrome 30)
if (!String.prototype.includes) { String.prototype.includes = function(s) { return this.indexOf(s) >= 0; }; }
if (!String.prototype.padStart) { String.prototype.padStart = function(len, ch) { var s = String(this); while (s.length < len) s = (ch || "0") + s; return s; }; }
var performancePreference="auto";
function hardwareNeedsLiteMode(){
  var cores=Number(navigator.hardwareConcurrency||0),memory=Number(navigator.deviceMemory||0),connection=navigator.connection||navigator.mozConnection||navigator.webkitConnection;
  return (cores>0&&cores<=4)||(memory>0&&memory<=4)||(connection&&connection.saveData===true)||(window.matchMedia&&window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}
function isLitePerformance(){return document.documentElement.classList.contains("performance-lite");}
function updatePerformanceButton(){
  var btn=document.getElementById("performanceBtn"),lite=isLitePerformance();if(!btn)return;
  btn.classList.toggle("is-lite",lite);btn.setAttribute("aria-pressed",lite?"true":"false");
  btn.textContent=performancePreference==="auto"?(lite?"⚡ 自动·流畅":"⚡ 性能自动"):(performancePreference==="lite"?"⚡ 流畅模式":"⚡ 标准模式");
  btn.title="自动模式会根据 CPU、内存和省流量设置选择；点击可手动切换";
}
function applyPerformanceMode(mode,persist){
  performancePreference=(mode==="lite"||mode==="standard")?mode:"auto";
  var lite=performancePreference==="lite"||(performancePreference==="auto"&&hardwareNeedsLiteMode());
  document.documentElement.classList.toggle("performance-lite",lite);
  if(persist){try{localStorage.setItem("oceanPerformanceMode",performancePreference);}catch(e){}}
  updatePerformanceButton();
}
function cyclePerformanceMode(){
  var next=performancePreference==="auto"?"standard":(performancePreference==="standard"?"lite":"auto");
  applyPerformanceMode(next,true);
  if(tideChart){tideChart.dispose();tideChart=null;if(lastChartRaw)renderChart(lastChartRaw,"",lastChartSite);}
}
function runWhenVisible(fn){return function(){if(!document.hidden)fn();};}
var dataLastRequested={tide:0,chart:0,weather:0,wave:0,offshore:0,fishing:0,alarms:0,typhoon:0,notifications:0};
var dataRefreshIntervals={tide:60*60*1000,chart:6*60*60*1000,weather:10*60*1000,wave:60*60*1000,offshore:15*60*1000,fishing:30*60*1000,alarms:5*60*1000,typhoon:60*60*1000,notifications:30*1000};
function markDataRequest(name){dataLastRequested[name]=Date.now();}
function dataRequestIsDue(name,now){return !dataLastRequested[name]||now-dataLastRequested[name]>=dataRefreshIntervals[name];}
function refreshDueData(){
  var now=Date.now();
  if(dataRequestIsDue("tide",now))loadTide();
  if(dataRequestIsDue("chart",now))loadChart();
  if(dataRequestIsDue("weather",now))loadWeather();
  if(dataRequestIsDue("wave",now))loadWave();
  if(dataRequestIsDue("offshore",now))loadOffshoreWave();
  if(dataRequestIsDue("fishing",now))loadFishing();
  if(dataRequestIsDue("alarms",now))loadCmaAlarm();
  if(dataRequestIsDue("typhoon",now))reloadTyphoonFrame();
  if(dataRequestIsDue("notifications",now))loadNotificationLogs();
}
(function initPerformanceMode(){
  var query=(location.search.match(/[?&]performance=(auto|lite|standard)(?:&|$)/)||[])[1],saved="";
  try{saved=localStorage.getItem("oceanPerformanceMode")||"";}catch(e){}
  applyPerformanceMode(query||saved||"auto",false);
  document.addEventListener("visibilitychange",function(){document.documentElement.classList.toggle("page-paused",document.hidden);if(!document.hidden){updateClock();refreshDueData();}});
})();
var tideRawData=null, tideChart=null, lastChartRaw=null, lastChartSite=null, lastChartPoints=[], lastTideList=[], resizeTimer=null, lastTideRising=null, soundEnabled=false, audioCtx=null, selectedDayOffset=0, tomorrowTideList=[], tomorrowTideReady=false;
var activeSituationPanel="typhoon",activeWeatherPanel="weather",activeOceanPanel="ocean",typhoonUpdatedAt="--",notificationUpdatedAt="--",oceanUpdatedAt="--",fishingUpdatedAt="--";
var stealthModuleClicks={weather:{count:0,last:0},situation:{count:0,last:0},ocean:{count:0,last:0}};
var $=function(id){return document.getElementById(id);};
function setText(id,text){var el=$(id); if(el) el.textContent=(text===null||text===undefined||text==="")?"--":text;}
function handleStealthModuleClick(group){
  var state=stealthModuleClicks[group];if(!state)return;
  var now=Date.now();state.count=(now-state.last<=1200)?state.count+1:1;state.last=now;
  if(state.count<5)return;
  state.count=0;state.last=0;
  if(group==="weather")switchWeatherPanel(activeWeatherPanel==="weather"?"sunset":"weather");
  else if(group==="situation")switchSituationPanel(activeSituationPanel==="typhoon"?"notifications":"typhoon");
  else if(group==="ocean")switchOceanPanel(activeOceanPanel==="ocean"?"fishing":"ocean");
}
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
  var f=$("typhoonFrame");
  if(!f)return;
  markDataRequest("typhoon");
  var now=new Date();
  typhoonUpdatedAt=String(now.getHours()).padStart(2,"0")+":"+String(now.getMinutes()).padStart(2,"0");
  updateSituationMeta();
  f.src="https://www.bhyb.org.cn/typhoon/?t="+Date.now();
}
function updateSituationMeta(){
  var showingNotifications=activeSituationPanel==="notifications";
  setText("situationSource",showingNotifications?"数据源：钉钉通知引擎":"数据源：北海预报减灾中心");
  setText("situationTime","更新 "+(showingNotifications?notificationUpdatedAt:typhoonUpdatedAt));
}
function switchSituationPanel(name){
  activeSituationPanel=name==="notifications"?"notifications":"typhoon";
  var showNotifications=activeSituationPanel==="notifications";
  var typhoonPanel=$("typhoonPanel"),notificationPanel=$("notificationPanel");
  var typhoonTab=$("typhoonTab"),notificationTab=$("notificationTab");
  if(typhoonPanel){typhoonPanel.hidden=showNotifications;typhoonPanel.classList.toggle("active",!showNotifications);}
  if(notificationPanel){notificationPanel.hidden=!showNotifications;notificationPanel.classList.toggle("active",showNotifications);}
  if(typhoonTab){typhoonTab.classList.toggle("active",!showNotifications);typhoonTab.setAttribute("aria-selected",showNotifications?"false":"true");}
  if(notificationTab){notificationTab.classList.toggle("active",showNotifications);notificationTab.setAttribute("aria-selected",showNotifications?"true":"false");}
  setText("situationModuleTitle",showNotifications?"钉钉通知记录":"台风路径与云图");
  var situationTitle=$("situationModuleTitle");if(situationTitle)situationTitle.setAttribute("aria-label","当前模块："+(showNotifications?"钉钉通知记录":"台风路径与云图"));
  updateSituationMeta();
  if(showNotifications&&dataRequestIsDue("notifications",Date.now()))loadNotificationLogs();
}
function switchWeatherPanel(name){
  activeWeatherPanel=name==="sunset"?"sunset":"weather";
  var showSunset=activeWeatherPanel==="sunset";
  var weatherPanel=$("weatherPanel"),sunsetPanel=$("sunsetPanel");
  var weatherTab=$("weatherTab"),sunsetTab=$("sunsetTab");
  if(weatherPanel){weatherPanel.hidden=showSunset;weatherPanel.classList.toggle("active",!showSunset);}
  if(sunsetPanel){sunsetPanel.hidden=!showSunset;sunsetPanel.classList.toggle("active",showSunset);}
  if(weatherTab){weatherTab.classList.toggle("active",!showSunset);weatherTab.setAttribute("aria-selected",showSunset?"false":"true");}
  if(sunsetTab){sunsetTab.classList.toggle("active",showSunset);sunsetTab.setAttribute("aria-selected",showSunset?"true":"false");}
  setText("weatherModuleTitle",showSunset?"晚霞评分":"实时天气与风况");
  var weatherTitle=$("weatherModuleTitle");if(weatherTitle)weatherTitle.setAttribute("aria-label","当前模块："+(showSunset?"晚霞评分":"实时天气与风况"));
  setText("weatherSource",showSunset?"数据源：Open-Meteo · 西侧4点云量":"数据源：Open-Meteo");
}
function updateOceanMeta(){
  var fishing=activeOceanPanel==="fishing";
  setText("oceanSource",fishing?"数据源：潮汐 · Open-Meteo · 青岛海洋预报":"数据源：全球潮汐 · 青岛海洋预报");
  var timeText=fishing?fishingUpdatedAt:oceanUpdatedAt;
  setText("tideUpdate",(!fishing&&String(timeText).indexOf("状态 ")===0?"":"更新 ")+timeText);
}
function switchOceanPanel(name){
  activeOceanPanel=name==="fishing"?"fishing":"ocean";
  var showFishing=activeOceanPanel==="fishing";
  var oceanPanel=$("oceanDataPanel"),fishingPanel=$("fishingPanel"),oceanTab=$("oceanDataTab"),fishingTab=$("fishingTab");
  if(oceanPanel){oceanPanel.hidden=showFishing;oceanPanel.classList.toggle("active",!showFishing);}
  if(fishingPanel){fishingPanel.hidden=!showFishing;fishingPanel.classList.toggle("active",showFishing);}
  if(oceanTab){oceanTab.classList.toggle("active",!showFishing);oceanTab.setAttribute("aria-selected",showFishing?"false":"true");}
  if(fishingTab){fishingTab.classList.toggle("active",showFishing);fishingTab.setAttribute("aria-selected",showFishing?"true":"false");}
  setText("oceanModuleTitle",showFishing?"钓鱼评分":"海况数据");
  var oceanTitle=$("oceanModuleTitle");if(oceanTitle)oceanTitle.setAttribute("aria-label","当前模块："+(showFishing?"钓鱼评分":"海况数据"));
  updateOceanMeta();
  if(showFishing&&dataRequestIsDue("fishing",Date.now()))loadFishing();
}
function formatNotificationTime(value){
  var text=String(value||"");
  var match=text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
  return match?{short:match[2]+"-"+match[3]+" "+match[4]+":"+match[5],full:match[1]+"-"+match[2]+"-"+match[3]+" "+match[4]+":"+match[5]+":"+(match[6]||"00")}:{short:"--",full:"--"};
}
function renderNotificationState(message,stateClass){
  var body=$("notificationTableBody");
  if(!body)return;
  body.textContent="";
  var row=document.createElement("tr"),cell=document.createElement("td");
  cell.colSpan=4;cell.className="notification-state "+(stateClass||"");cell.textContent=message;
  row.appendChild(cell);body.appendChild(row);
}
function renderNotificationLogs(items){
  var body=$("notificationTableBody");
  if(!body)return;
  var rows=Array.isArray(items)?items.slice():[];
  rows.sort(function(a,b){return String(b.sent_at||"").localeCompare(String(a.sent_at||""));});
  setText("notificationCount",String(rows.length));
  if(!rows.length){renderNotificationState("暂无已发送通知，规则成功触发后会显示在这里","is-empty");return;}
  body.textContent="";
  rows.forEach(function(item){
    var row=document.createElement("tr");
    var timeCell=document.createElement("td"),ruleCell=document.createElement("td"),roleCell=document.createElement("td"),messageCell=document.createElement("td");
    var time=formatNotificationTime(item.sent_at);
    timeCell.className="notification-time";timeCell.dataset.label="发送时间";timeCell.textContent=time.short;timeCell.title=time.full;
    ruleCell.className="notification-rule";ruleCell.dataset.label="通知规则";
    var ruleName=document.createElement("strong");ruleName.textContent=item.rule_name||"未命名规则";ruleCell.appendChild(ruleName);
    roleCell.className="notification-roles";roleCell.dataset.label="通知角色";
    var roles=Array.isArray(item.role_names)?item.role_names:[];
    if(!roles.length){roleCell.textContent="--";}else{roles.forEach(function(role){var tag=document.createElement("span");tag.className="notification-role";tag.textContent=role;roleCell.appendChild(tag);});}
    messageCell.className="notification-message";messageCell.dataset.label="通知内容";
    var message=String(item.message||"--").replace(/\s+/g," ").trim();
    var messageText=document.createElement("div");messageText.className="notification-message-text";messageText.textContent=message;messageCell.title=message;messageCell.appendChild(messageText);
    row.appendChild(timeCell);row.appendChild(ruleCell);row.appendChild(roleCell);row.appendChild(messageCell);body.appendChild(row);
  });
}
function loadNotificationLogs(force){
  if(!force&&!dataRequestIsDue("notifications",Date.now()))return;
  markDataRequest("notifications");
  var btn=$("notificationRefreshBtn");
  if(btn){btn.disabled=true;btn.textContent="读取中";}
  fetchJSON("/api/notification/public-logs?limit=30",10000,function(e,r){
    if(btn){btn.disabled=false;btn.textContent="刷新记录";}
    if(e||!r||!r.success||!Array.isArray(r.data)){renderNotificationState("通知记录暂时无法读取，请稍后重试","is-error");return;}
    notificationUpdatedAt=r.updateTime||"--";updateSituationMeta();renderNotificationLogs(r.data);
  });
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
  renderSunsetScore(obj&&obj.sunset);
}
function sunsetSunLevel(score){
  if(score>=85)return 5;
  if(score>=70)return 4;
  if(score>=45)return 3;
  if(score>=25)return 2;
  return score>=0?1:0;
}
function setSunsetComponent(id,value,maxValue){
  var el=$(id),valid=value!==null&&value!==undefined&&!isNaN(Number(value));
  if(el)el.innerHTML=(valid?Math.round(Number(value)):"--")+"<small>/"+maxValue+"</small>";
}
function renderSunsetScore(sunset){
  sunset=sunset||{};
  var score=Number(sunset.score),available=sunset.score!==null&&sunset.score!==undefined&&!isNaN(score);
  setText("sunsetScore",available?Math.round(score):"--");
  var level=available?sunsetSunLevel(score):0;
  setText("sunsetLevelText",available?(sunset.level_text||"已评分")+" · "+level+"级小太阳":(sunset.level_text||"数据不足"));
  setText("sunsetTime",sunset.sunset_time||"--");
  setText("sunsetWindow",sunset.window_text||"--");
  setText("sunsetReason",sunset.reason||"晚霞预测数据暂不可用");
  var components=sunset.components||{};
  setSunsetComponent("sunsetLightScore",components.light_corridor,35);
  setSunsetComponent("sunsetCloudScore",components.cloud_canvas,30);
  setSunsetComponent("sunsetAirScore",components.transparency,15);
  setSunsetComponent("sunsetRainScore",components.precipitation,10);
  setSunsetComponent("sunsetTrendScore",components.cloud_trend,10);
  var suns=$("sunsetSuns"),tokens=suns?suns.querySelectorAll(".sun-token"):[];
  for(var i=0;i<tokens.length;i++)tokens[i].classList.toggle("active",i<level);
  if(suns)suns.setAttribute("aria-label",available?"晚霞等级"+level+"颗小太阳，共5颗":"晚霞等级暂不可用");
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
function fishingTimeParts(value){
  var match=String(value||"").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if(!match)return {date:"--",time:"--",full:"--"};
  var today=new Date(),same=Number(match[2])===today.getMonth()+1&&Number(match[3])===today.getDate();
  return {date:same?"今日":match[2]+"-"+match[3],time:match[4]+":"+match[5],full:match[2]+"-"+match[3]+" "+match[4]+":"+match[5]};
}
function setFishingComponent(id,value,max){
  var el=$(id);if(!el)return;el.textContent=value===null||value===undefined?"--":String(value);
  var small=document.createElement("small");small.textContent="/"+max;el.appendChild(small);
}
function renderFishing(obj,updateTime){
  if(!obj)return;
  fishingUpdatedAt=updateTime||"--";if(activeOceanPanel==="fishing")updateOceanMeta();
  var best=obj.best_snapshot||{},components=best.components||{},bestTime=fishingTimeParts(obj.best_hour);
  setText("fishingScore",obj.score);setText("fishingLevel",obj.level||"数据不足");
  setText("fishingBestTime","最佳 "+bestTime.date+" "+bestTime.time);
  setText("fishingPhase",(best.phase||"潮况 --")+(best.tide_change_cm_h!==undefined?" · "+best.tide_change_cm_h+"cm/h":""));
  setText("fishingWeather",(best.weather||"天气 --")+(best.temperature_c!==null&&best.temperature_c!==undefined?" · "+best.temperature_c+"℃":""));
  setFishingComponent("fishingTideScore",components.tide,30);setFishingComponent("fishingWindScore",components.wind,25);setFishingComponent("fishingWaveScore",components.wave,20);setFishingComponent("fishingRainScore",components.rain,15);setFishingComponent("fishingLightScore",components.light,10);
  setText("fishingTideHeight",best.tide_height_cm===undefined?"--":best.tide_height_cm+"cm");
  setText("fishingWind",best.wind_kmh===null||best.wind_kmh===undefined?"--":best.wind_kmh+"km/h");
  setText("fishingWave",best.wave_height_m===null||best.wave_height_m===undefined?"缺失":best.wave_height_m+"m");
  setText("fishingRain",best.precipitation_probability===null||best.precipitation_probability===undefined?"--":best.precipitation_probability+"%");
  var air=best.temperature_c===null||best.temperature_c===undefined?"--":best.temperature_c+"℃",water=obj.water_temp_c===null||obj.water_temp_c===undefined?"--":obj.water_temp_c+"℃";
  setText("fishingTemperature",air+" / "+water);
  var windows=$("fishingWindows");if(windows){windows.textContent="";var items=Array.isArray(obj.windows)?obj.windows:[];
    if(!items.length){var empty=document.createElement("div");empty.className="fishing-empty";empty.textContent="未来24小时暂无安全且评分达65分的连续时段";windows.appendChild(empty);}
    else{items.forEach(function(item){var start=fishingTimeParts(item.start),end=fishingTimeParts(item.end),card=document.createElement("div"),title=document.createElement("strong"),reason=document.createElement("span");card.className="fishing-window";title.textContent=start.date+" "+start.time+"–"+end.time+" · "+item.score+"分";reason.textContent=item.reason||item.level||"适合";card.appendChild(title);card.appendChild(reason);windows.appendChild(card);});}
  }
  var warning=$("fishingWarning");if(warning){warning.textContent=(obj.warning||"")+" · "+(obj.method_note||"");warning.classList.toggle("is-danger",best.safety!=="normal");}
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
  tideRawData=res;oceanUpdatedAt=upTime||"--";if(activeOceanPanel==="ocean")updateOceanMeta();setText("globalUpdate","数据更新 "+(upTime||"--"));
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
    oceanUpdatedAt="状态 "+statusTime;if(activeOceanPanel==="ocean"){setText("tideUpdate",oceanUpdatedAt);}
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
  var phaseAvailable=false;
  if(next){
    phaseAvailable=true;
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
    // 背景水位表达实际潮位方向：涨潮随进度升高，退潮随进度降低。
    // 明日预报或当前潮段不可计算时保持为空，避免无数据状态被误填满。
    var waterHeight=selectedDayOffset===0&&phaseAvailable
      ? (rising?(phaseProgress||0):100-(phaseProgress||0))
      : 0;
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
    points.push({dateKey:last.dateKey,label:"23:59",minute:1439,value:last.value,pointType:"hour",extremaType:""});
  }
  return points;
}
function adaptiveChartFont(base,min,max){var scale=Math.min(window.innerWidth/1280,window.innerHeight/760);return Math.max(min,Math.min(max,Math.round(base*scale*1.4)));}
function initChart(){
  if(typeof echarts==="undefined")return false; if(!tideChart){tideChart=echarts.init($("tideChart"),null,{devicePixelRatio:isLitePerformance()?1:Math.min(window.devicePixelRatio||1,2)}); window.addEventListener("resize",function(){clearTimeout(resizeTimer);resizeTimer=setTimeout(function(){if(tideChart)tideChart.resize();},180);},{passive:true});}
  return true;
}
function renderChart(rawArr,msg,site){
  lastChartRaw=Array.isArray(rawArr)?rawArr:null; lastChartSite=site||null; var points=parseChartPoints(rawArr);
  lastChartPoints=points;
  if(lastTideList.length) calcTideStatus(lastTideList);
  setText("chartSource","数据源：全球潮汐平台"+(site&&site.code?" · "+site.name+"("+site.code+")":""));
  if(!points.length){$("tideChart").innerText=msg||"暂无实时曲线数据";return;}
  if(!initChart()){ $("tideChart").innerText="ECharts 加载中"; return; }
  var axisFont=adaptiveChartFont(14,11,18), markFont=adaptiveChartFont(13,10,16), nameFont=adaptiveChartFont(15,12,19);
  var maxVal=Math.max.apply(null,points.map(function(p){return p.value;}));
  var isMobileView=document.documentElement.classList.contains("mobile");
  var gridLeft=isMobileView?30:52, gridRight=isMobileView?30:52, gridTop=isMobileView?36:48, gridBottom=isMobileView?32:40;
  var litePerformance=isLitePerformance();
  var markData=points.filter(function(p){return p.pointType==="extrema";}).map(function(p){var isHigh=p.extremaType==="满潮";return {name:p.extremaType,coord:[p.label,p.value],value:p.value,labelText:p.extremaType+" "+p.label+"\n"+p.value+"cm",itemStyle:{color:isHigh?"#ff5252":"#00e676",shadowBlur:litePerformance?0:8,shadowColor:isHigh?"rgba(255,82,82,.6)":"rgba(0,230,118,.6)"},label:{formatter:function(params){return params.data.labelText;},color:isHigh?"#ff5252":"#00e676",fontSize:markFont,fontWeight:"bold",lineHeight:markFont+1,position:"right",distance:4,offset:[0,-14],textShadowColor:"rgba(0,0,0,.85)",textShadowBlur:litePerformance?0:4,textShadowOffsetX:0,textShadowOffsetY:1}};});
  tideChart.setOption({
    animation:!litePerformance,
    backgroundColor:"transparent",
    tooltip:{trigger:"axis",formatter:function(p){return "时间："+p[0].axisValue+"<br>潮高："+p[0].value+" cm";},backgroundColor:"rgba(15,21,40,.94)",borderColor:"rgba(0,229,255,.35)",textStyle:{color:"#e8eaf6",fontSize:axisFont}},
    grid:{left:gridLeft,right:gridRight,top:gridTop,bottom:gridBottom,containLabel:true},
    xAxis:{type:"category",data:points.map(function(p){return p.label;}),axisLabel:{rotate:0,interval:2,fontSize:axisFont,margin:6,color:"rgba(232,234,246,.75)",textShadowColor:"rgba(0,0,0,.7)",textShadowBlur:4,textShadowOffsetX:0,textShadowOffsetY:1},axisLine:{lineStyle:{color:"rgba(0,229,255,.28)"}},axisTick:{lineStyle:{color:"rgba(0,229,255,.22)"}}},
    yAxis:{name:"潮高(cm)",type:"value",max:Math.ceil((maxVal+35)/50)*50,nameTextStyle:{fontSize:nameFont,color:"rgba(232,234,246,.75)",textShadowColor:"rgba(0,0,0,.7)",textShadowBlur:4,textShadowOffsetX:0,textShadowOffsetY:1},axisLabel:{fontSize:axisFont,color:"rgba(232,234,246,.75)",textShadowColor:"rgba(0,0,0,.7)",textShadowBlur:4,textShadowOffsetX:0,textShadowOffsetY:1},axisLine:{lineStyle:{color:"rgba(0,229,255,.28)"}},splitLine:{lineStyle:{color:"rgba(255,255,255,.07)"}}},
    series:[{name:"潮高",type:"line",data:points.map(function(p){return p.value;}),smooth:!litePerformance,symbolSize:litePerformance?4:6,itemStyle:{color:"#00e5ff",shadowBlur:litePerformance?0:8,shadowColor:"rgba(0,229,255,.5)"},lineStyle:{color:"#00e5ff",width:3,shadowBlur:litePerformance?0:10,shadowColor:"rgba(0,229,255,.55)"},areaStyle:{color:{type:"linear",colorStops:[{offset:0,color:"rgba(0,229,255,.28)"},{offset:1,color:"rgba(0,229,255,.03)"}]}},markPoint:{symbol:"circle",symbolSize:litePerformance?18:24,data:markData},markLine:{symbol:"none",silent:true,data:[],lineStyle:{color:"#ffab00",width:2,type:"solid",shadowBlur:litePerformance?0:8,shadowColor:"rgba(255,171,0,.6)"},label:{show:true,formatter:"现在",color:"#ffab00",fontSize:markFont,fontWeight:"bold",position:"end",distance:[4,0],backgroundColor:"rgba(6,10,20,.85)",padding:[3,8,3,8],borderRadius:3,textShadowColor:"rgba(0,0,0,.85)",textShadowBlur:litePerformance?0:4,textShadowOffsetX:0,textShadowOffsetY:1}}}]
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
  renderSunsetScore({level_text:"仅展示今日",reason:"晚霞评分当前仅展示今日实时预测"});
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
function loadWeather(){markDataRequest("weather");fetchJSON(apiUrl("/api/weather"),20000,function(e,r){if(r&&r.tomorrow_unavailable){showWeatherUnavailable();return;}if(r&&r.data)renderWeather(r.data,r.updateTime);});}
function loadWave(){markDataRequest("wave");fetchJSON(apiUrl("/api/wave"),20000,function(e,r){if(r&&r.tomorrow_unavailable){showWaveUnavailable();return;}if(r&&r.data)renderWave(r.data,r.updateTime);});}
function setOffshoreWaveState(text,isError,detail){
  var el=$("offshoreWaveHeight");
  if(!el)return;
  el.textContent=text||"--";
  el.classList.toggle("data-error",!!isError);
  el.title=detail||"";
  el.setAttribute("aria-label",detail||("近海浪高 "+(text||"--")));
}
function loadOffshoreWave(){markDataRequest("offshore");fetchJSON(apiUrl("/api/offshore_wave"),20000,function(e,r){
  if(e||!r){setOffshoreWaveState("请求失败",true,"无法连接青岛浪高接口");return;}
  if(!r.success||!r.data){
    var label=String(r.error_code)==="502"?"502 异常":"接口异常";
    setOffshoreWaveState(label,true,r.msg||"青岛浪高接口异常");
    return;
  }
  setOffshoreWaveState(r.data.wave_height||"--",false,"青岛近海浪高");
});}
function loadFishing(){
  markDataRequest("fishing");
  fetchJSON(apiUrl("/api/fishing"),30000,function(e,r){
    if(r&&r.data){renderFishing(r.data,r.updateTime);return;}
    fishingUpdatedAt="失败";if(activeOceanPanel==="fishing")updateOceanMeta();
    var windows=$("fishingWindows");if(windows){windows.innerHTML='<div class="fishing-empty">钓鱼评分暂不可用，请稍后重试</div>';}
    var warning=$("fishingWarning");if(warning){warning.textContent=(r&&r.msg)||"无法连接钓鱼评分接口";warning.classList.add("is-danger");}
  });
}
function loadAlarm(){fetchJSON(apiUrl("/api/alarm"),20000,function(e,r){});}
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
  markDataRequest("alarms");
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
function loadTide(){markDataRequest("tide");fetchJSON(apiUrl("/api/tide"),20000,function(e,r){if(!r||e)return;if(r&&r.tomorrow_unavailable){showTideUnavailable();return;}if(r&&r.data)renderTide(r,r.updateTime);});
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
function loadChart(){markDataRequest("chart");fetchJSON(apiUrl("/api/tideChart"),20000,function(e,r){if(!r||e){renderChart([],"潮汐曲线加载失败",null);return;}if(r&&r.tomorrow_unavailable){showChartUnavailable();return;}setText("chartTime","更新 "+(r.updateTime||"--"));renderChart(r.chart,r.msg,r.site);});}
function refreshAllData(){
  var btn=$("refreshBtn");
  if(btn){btn.disabled=true;btn.classList.add("is-loading");btn.textContent="↻ 刷新中";}
  loadTide();loadChart();loadWeather();loadWave();loadOffshoreWave();loadFishing();loadCmaAlarm();loadNotificationLogs(true);
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
  updatePerformanceButton();updateClock(); setInterval(runWhenVisible(updateClock),1000);
  updateDayButtons();
  markDataRequest("typhoon");
  var typhoonNow=new Date();typhoonUpdatedAt=String(typhoonNow.getHours()).padStart(2,"0")+":"+String(typhoonNow.getMinutes()).padStart(2,"0");updateSituationMeta();
  loadTide(); loadChart(); loadWeather(); loadWave(); loadOffshoreWave(); loadFishing(); loadCmaAlarm(); loadNotificationLogs(true);
  setInterval(runWhenVisible(loadTide),dataRefreshIntervals.tide); setInterval(runWhenVisible(loadChart),dataRefreshIntervals.chart); setInterval(runWhenVisible(loadWeather),dataRefreshIntervals.weather); setInterval(runWhenVisible(loadWave),dataRefreshIntervals.wave); setInterval(runWhenVisible(loadOffshoreWave),dataRefreshIntervals.offshore); setInterval(runWhenVisible(loadFishing),dataRefreshIntervals.fishing); setInterval(runWhenVisible(loadCmaAlarm),dataRefreshIntervals.alarms); setInterval(runWhenVisible(reloadTyphoonFrame),dataRefreshIntervals.typhoon); setInterval(runWhenVisible(loadNotificationLogs),dataRefreshIntervals.notifications);
  setInterval(runWhenVisible(function(){if(lastTideList.length)calcTideStatus(lastTideList);}),60*1000);
  setTimeout(function(){if(lastChartRaw)renderChart(lastChartRaw,"",lastChartSite);},1000);
}
boot();
