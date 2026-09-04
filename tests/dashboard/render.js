/* Render the real page with a dataset covering every status group, so the
   grouping CSS can be looked at rather than assumed. */
const fs = require('fs'), vm = require('vm');
const HEAD = ['Category','Company','Role','Score','Location','Link','Priority','Deadline',
              'Opens','Duration','Status','Notes','Description','Score reason','Source'];
const rows = [
  ['Software','Renault Group','CS27 Data Analyst (H/F)','85','Paris, France','https://x/1','PRIORITY','12/09/2026','','6 months','Interview','','Data analysis on connected-vehicle telemetry.','Strong motorsport-adjacent fit.','adzuna'],
  ['Software','Mistral AI','Applied AI Intern','82','Paris, France','https://x/2','','','','6 months','OA','','Forward-deployed ML engineering.','AI lab, exactly the target.','lever'],
  ['Software','Figma','SWE Intern - Winter 2027','80','London, UK','https://x/3','','25/08/2026','','12 weeks','Not applied','','Build design tooling used by millions.','Product engineering at scale.','greenhouse'],
  ['Software','Epic Games','Gameplay Programmer Intern','80','London, UK','https://x/4','','','','12 weeks','Not applied','','C++ gameplay systems.','Games + graphics.','greenhouse'],
  ['Software','Porsche','IT, Data and Analytics Intern','82','Atlanta, USA','https://x/5','','','','6 months','Applied','','Data platform work for Porsche Cars.','Automotive passion employer.','adzuna'],
  ['Software','Lucid Motors','Data Analyst Intern','82','Newark, USA','https://x/6','','','','6 months','Applied','','EV powertrain data analysis.','EV/automotive.','adzuna'],
  ['Software','TikTok','Backend SWE Intern','80','London, UK','https://x/7','','','','12 weeks','Offer','','Backend systems for TikTok LIVE.','Big tech, strong brand.','careers'],
  ['Software','DataRobot','Agentic AI Intern','42','Remote','https://x/8','','','','','Rejected','','Agent tooling.','Weak fit.','adzuna']];
function makeSheet(name, data) {
  function range(r,c,nr,nc){return{
    getDisplayValues:()=>{const o=[];for(let i=0;i<nr;i++){const row=data[r-1+i]||[];const s=[];
      for(let k=0;k<nc;k++)s.push(row[c-1+k]===undefined?'':String(row[c-1+k]));o.push(s);}return o;},
    getDisplayValue:()=>String((data[r-1]||[])[c-1]||''),setValue(){},setValues(){},
    createTextFinder:()=>({matchCase(){return this;},findAll:()=>[]})};}
  return {getName:()=>name,getLastRow:()=>data.length,getLastColumn:()=>data[0].length,
          getDataRange:()=>range(1,1,data.length,data[0].length),
          getRange:(r,c,nr,nc)=>range(r,c,nr===undefined?1:nr,nc===undefined?1:nc),
          deleteRow(){},_data:data};
}
const sheets = {
  'Job Bank': makeSheet('Job Bank',[HEAD,...rows]),
  'Archive': makeSheet('Archive',[['Added','Score','Category','Company','Role','Location','Deadline','Link','Source','Disposition','Description','Duration']]),
  'Opening Soon': makeSheet('Opening Soon',[['Company','Programme','Expected open date','Expected close date','Status','Link','Notes'],
    ['European Space Agency','Graduate Trainee Programme 2027','2027-02-01','2027-03-31','Added by hand','https://jobs.esa.int','Opens around February.'],
    ['Mercedes-AMG F1','Industrial Placement (Software/Data)','2026-09-15','2026-11-30','Predicted from last year','https://mercedesamgf1.com/careers','']]),
  'Watchlist': makeSheet('Watchlist',[['Company','ATS','Slug / URL','Notes']])
};
const sandbox = { console, SpreadsheetApp:{getActiveSpreadsheet:()=>({getUrl:()=>'https://sheet',
    getSheetByName:n=>sheets[n]||null,insertSheet:n=>sheets[n]})},
  Utilities:{formatDate:()=>'Monday 17 August'}, Session:{getScriptTimeZone:()=>'UTC'},
  PropertiesService:{getScriptProperties:()=>({getProperty:()=>null,setProperty(){}})},
  UrlFetchApp:{fetch(){throw new Error('x');},fetchAll:()=>[]},
  HtmlService:{createHtmlOutput:()=>({setTitle:()=>({addMetaTag:()=>({})})})}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'), sandbox);
fs.writeFileSync(process.argv[3], sandbox.renderPage());
console.log('wrote', process.argv[3]);
