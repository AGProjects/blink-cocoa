for i in *.py *.html; 
   do cat $i | awk '{sub(/[ \t]+$/, "")};1' > $i.trimmed
   mv $i.trimmed $i
done
