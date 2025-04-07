
import * as cheerio from 'cheerio';
import * as fs from 'fs';

const months = {
  "Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06","Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12",
};

function updateData(dataObj){
  if(typeof data_ga[dataObj.ga]!== "undefined"){
    data_ga[dataObj.ga].push(dataObj)
  } else {
    data_ga[dataObj.ga] = [dataObj];
  }
  console.log(data_ga.length);
  //   const content = Object.values(dataObj).toString();
  //   try {
  //   fs.writeFileSync('lectureData.txt', content, { flag: 'a' });
  //   // file written successfully
  // } catch (err) {
  //   console.error(err);
  // }
}
function make_years(s,e){
  const years = [];
  for(let i=s;i<=e;i++){
    years.push(i);
  }
  return years;
}

const data_ga =[];

async function scrapeHTML(url,year) {
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const res = [];
    const html = await response.text();
    const $ = cheerio.load(html);
    console.log("Working on: "+ url);
    const lectures = $(".articleBox > table.databaseTable > tbody > tr");
    // console.log(`Found:${lectures.length} Lecures`);
    lectures.each((index, element) => {
      let lecture = $(element).children('td');
      for(let i=0; i<lecture.length;i++){
        const lectureDates = $(lecture[0]).text().split(" ");
        const lectureDate = months[lectureDates[1]]+"/"+lectureDates[0]+"/"+year;
        const ga = $(lecture[4]).text();
        const location = $(lecture[3]).text() || "no_location";
        const lectureTitle = $(lecture[1]).text();
        const lectureURL = $(lecture[1]).find('a').attr('href');
        const lectureCycle = $(lecture[2]).text();
        const lectureCycleURL = $(lecture[2]).find('a').attr('href');
        const content = {
              ga: ga,
              date: lectureDate,
              title: lectureTitle,
              location: location,
              lectureUrl: lectureURL,
              lectureCycleURL:lectureCycleURL          
        };
        fs.writeFileSync('lectureData.txt', Object.values(content).toString()+"\n", { flag: 'a' });
        // res.push(ga,lectureTitle,location,lectureURL,lectureCycleURL);
        // res.push {
        //       ga: ga,
        //       title: lectureTitle,
        //       location: location,
        //       lectureUrl: lectureURL,
        //       lectureCycleURL:lectureCycleURL          
        // };
        // data.push(ga,lectureURL,lectureCycleURL);

      }  
      // return res;
    });

  } catch (error) {
    console.error("Error during scraping:", error);
    return null;
  }
}

// Usage example:
const years = make_years(1884, 1925);
// const lectureList = [];
for(let y=0;y<years.length;y++){
  const year = years[y];
  const url = 'https://rsarchive.org/Dates.php?year='+year;
  scrapeHTML(url,year).then(res=>{
    if(res){
      // console.log(Object.value(res).toString());
      console.log("res:",res);
    }
  });
}