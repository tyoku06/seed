<?php
function fizzBuzz ($i) {
            switch (true) {
                case $i % 3 === 0 && $i % 5 === 0:
                    return 'FuzzBuzz';
                    break;

                case $i % 3 === 0:
                    return 'Buzz';
                    break;

                case $i % 5 === 0:
                    return 'Fizz';
                    break;
                
                default:
                    return $i;
                    break;
            }
        }

        for ($i = 1; $i <= 100; $i++) {
             echo fizzBuzz($i).' ';
        }');
        
   console.log($i);
   ?>
