
import * as cheerio from 'cheerio';

async function scrapeHTML(url) {
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const html = await response.text();
    const $ = cheerio.load(html);

    const data = [];

    const months = {
      "Jan":"01",
      "Feb":"02",
      "Mar":"03",
      "Apr":"04",
      "May":"05",
      "Jun":"06",
      "Jul":"07",
      "Aug":"08",
      "Sep":"09",
      "Oct":"10",
      "Nov":"11",
      "Dec":"12",
    };

    const lectures = $(".articleBox > table.databaseTable > tbody > tr");
    lectures.each((index, element) => {
      let lecture = $(element).children('td');
      for(let i=0; i<lecture.length;i++){
        const lectureDates = $(lecture[0]).text().split(" ");
        const lectureDate = months[lectureDates[1]]+"/"+lectureDates[0]+"/1912";
        const ga = $(lecture[4]).text();
        const location = $(lecture[3]).text();
        const lectureTitle = $(lecture[1]).text();
        const lectureURL = $(lecture[1]).find('a').attr('href');
        const lectureCycle = $(lecture[2]).text();
        const lectureCycleURL = $(lecture[2]).find('a').attr('href');
        // data.push(lectureURL,lectureCycleURL);
      }  
    });

    return data;
    
  } catch (error) {
    console.error("Error during scraping:", error);
    return null;
  }
}

// Usage example:
const url = 'https://rsarchive.org/Dates.php?year=1912';
scrapeHTML(url).then(data => {
  if (data) {
    console.log("Scraped data:", data);
  }
});