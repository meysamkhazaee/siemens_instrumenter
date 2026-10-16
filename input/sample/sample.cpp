
#include <iostream>
#include <cstdlib>
using namespace std;

int main(int argc, char* argv[]) 
{
    if (argc != 9) 
    {
        cout << "Usage: " << argv[0]
             << " a b c d e f g h" << endl;
        return 1;
    }

    int x1[2][2], x2[2][2], x3[2][2];
    int flag1 = 0, flag2 = 0;

    x1[0][0] = atoi(argv[1]);
    x1[0][1] = atoi(argv[2]);
    x1[1][0] = atoi(argv[3]);
    x1[1][1] = atoi(argv[4]);

    x2[0][0] = atoi(argv[5]);
    x2[0][1] = atoi(argv[6]);
    x2[1][0] = atoi(argv[7]);
    x2[1][1] = atoi(argv[8]);

    int det1 = x1[0][0] * x1[1][1]
             - x1[0][1] * x1[1][0];

    if (det1 != 0) 
    {
        flag1 = 1;

        for (int i = 0; i < 2; i++) 
        {
            for (int j = 0; j < 2; j++) 
            {
                x3[i][j] = 0;

                for (int k = 0; k < 2; k++) 
                {
                    x3[i][j] += x1[i][k] * x2[k][j];
                }
            }
        }

        if (flag1 > 0) 
        {
            int det2 = x2[0][0] * x2[1][1]
                     - x2[0][1] * x2[1][0];

            if (det2 != 0) 
            {
                flag2 = 1;

                if (x3[0][0] != 1 ||
                    x3[1][1] != 1 ||
                    x3[0][1] != 0 ||
                    x3[1][0] != 0) 
                {
                    cout << "Both are invertible but x1 is not the inverse of x2" << endl;
                } 
                else 
                {
                    cout << "x1 is the inverse of x2" << endl;
                }
            } 
            else 
            {
                cout << "x2 is not invertible" << endl;
            }
        }

        if (flag2 > 0) 
        {
            int det3 = x3[0][0] * x3[1][1]
                     - x3[0][1] * x3[1][0];

            if (det3 != 0) 
            {
                cout << "The multiplication result of x1 and x2 is invertible" << endl;
            } 
            else 
            {
                cout << "The multiplication result of x1 and x2 is not invertible" << endl;
            }
        }
    } 
    else 
    {
        cout << "x1 is not invertible" << endl;
    }

    return 0;
}
